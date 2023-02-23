import os
from copy import deepcopy
from dataclasses import dataclass
import pyrallis
import torch
import torch.nn as nn
from torch.distributions import Normal
import numpy as np
import wandb
from tqdm import trange
import time
import random
import d4rl
import gym



@dataclass
class TrainConfig:
    total_updates: int = 10  # number of total updates
    batch_size: int = 2048  # batch size (per update)
    eval_every: int = 100  # evaluate every n updates
    eval_episodes: int = 10  # number of episodes for evaluation
    lr_actor: float = 0.0003  # learning rate for actor
    lr_critic: float = 0.0003  # learning rate for critic
    env: str = 'halfcheetah-medium-v2'  # environment name

    num_critics: int = 5  # number of critics
    eta: float = 0.1  # eta for diversity loss
    gamma: float = 0.99  # discount factor
    tau: float = 0.005  # tau for target network update

    name: str = 'edac'  # wandb name of the experiment
    group: str = 'edac'  # wandb group name
    project: str = 'edac_reimplementation'  # wandb project name
    seed: int = 0  # seed (0 for random seed)

    def __post_init__(self):
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.name = f'{self.name}-{self.env}-{time.strftime("%y%m%d-%H%M%S")}'


class ReplayBuffer:
    '''buffer for the offline RL dataset'''
    def __init__(self, dataset, batch_size : int, device : str):
        size = dataset["observations"].shape[0]
        if size < batch_size:
            raise ValueError(f'The batch_size ({batch_size}) cannot be larger than the dataset size ({size}).')
        self.batch_size = batch_size
        self.states, self.actions, self.rewards, self.next_states, self.dones = (
            torch.tensor(dataset[x], dtype=torch.float32, device=device)
            for x in ['observations', 'actions', 'rewards', 'next_observations', 'terminals']
        )

    def sample(self):
        idx = np.random.randint(0, len(self.states))
        return (
            self.states[idx],
            self.actions[idx],
            self.rewards[idx],
            self.next_states[idx],
            self.dones[idx],
        )


class Actor(nn.Module):
    def __init__(self, layer_sizes : list[int], max_action : float = 1.0):
        super().__init__()
        assert len(layer_sizes) >= 3, f'layer_sizes must have at least 3 elements (input, hidden, output), got {layer_sizes}'
        self.max_action = max_action
        # setup hidden layers based on the given layer sizes
        self.hidden = nn.Sequential(*(
            x for i in range(len(layer_sizes) - 2) for x in [
                nn.Linear(layer_sizes[i], layer_sizes[i + 1]),
                nn.ReLU()
            ]
        ))
        # create output and output uncertainty layers
        self.output = nn.Linear(layer_sizes[-2], layer_sizes[-1])
        self.output_uncertainty = nn.Linear(layer_sizes[-2], layer_sizes[-1])

    def forward(self, state : torch.Tensor):
        x_hidden = self.hidden(state)
        x_mean = self.output(x_hidden)
        x_std = torch.exp(torch.clip(self.output_uncertainty(x_hidden), -5, 2))
        policy_dist = Normal(x_mean, x_std)
        action_linear = policy_dist.rsample()
        action = torch.tanh(action_linear) * self.max_action
        action_log_prob = policy_dist.log_prob(action_linear).sum(-1)
        # TODO: maybe subtract `torch.log(1 - tanh_action.pow(2) + 1e-6).sum(axis=-1)` from action_log_prob, as in CORL
        return action, action_log_prob


class VectorCritic(nn.Module):
    def __init__(self, layer_sizes: list[int], num_critics: int):
        super().__init__()
        self.models = []
        for _ in range(num_critics):
            self.models.append(
                nn.Sequential(*(
                    x for i in range(len(layer_sizes) - 1) for x in [
                        nn.Linear(layer_sizes[i], layer_sizes[i + 1]),
                        nn.ReLU()
                    ]
                ))
            )
    
    def forward(self, state: torch.Tensor, action: torch.Tensor):
        return torch.cat([model(torch.cat([state, action], dim=-1)) for model in self.models], dim=-1)
        # return [model(torch.cat([state, action], dim=-1)) for model in self.models]


@pyrallis.wrap()
def train(config: TrainConfig) -> None:
    # init env
    eval_env = gym.make(config.env)
    state_dim = eval_env.observation_space.shape[0]
    action_dim = eval_env.action_space.shape[0]
    d4rl_dataset = d4rl.qlearning_dataset(eval_env)
    buffer = ReplayBuffer(d4rl_dataset, config.batch_size, config.device)

    # set seed
    if config.seed == 0:
        config.seed = np.random.randint(0, 100000)
    else:
        # if a seed is given, try to be deterministic
        torch.use_deterministic_algorithms(True)
    eval_env.seed(config.seed)
    eval_env.action_space.seed(config.seed)
    os.environ["PYTHONHASHSEED"] = str(config.seed)
    np.random.seed(config.seed)
    random.seed(config.seed)
    torch.manual_seed(config.seed)

    # init wandb
    wandb_run = wandb.init(name=config.name, group=config.group, project=config.project, config=config)

    # init model
    actor = Actor([state_dim, 256, 256, action_dim], max_action=eval_env.action_space.high[0]).to(config.device)
    actor_optimizer = torch.optim.Adam(actor.parameters(), lr=config.lr_actor)
    critic = VectorCritic([state_dim + action_dim, 256, 256, 1], num_critics=config.num_critics).to(config.device)
    critic_optimizer = torch.optim.Adam(critic.parameters(), lr=config.lr_critic)
    with torch.no_grad: target_critic = deepcopy(critic)
    # TODO: maybe also train config.beta, as in the CORL implementation (there its called alpha),
    #       but the paper pseudocode doesn't do that.
    #       the official implementaiton does that only if `use_automatic_entropy_tuning` is enabled

    # main training loop
    for update_step in trange(config.total_updates):
        # [batch_size, ...]
        state, action, reward, next_state, done = buffer.sample()
        with torch.no_grad():
            # [?], [?]
            next_action, next_action_log_prob = actor(next_state)
            # [?]
            q_next = target_critic(next_state, next_action).min().values - config.beta * next_action_log_prob
            # [batch_size]
            q_target = reward + (1 - done) * config.gamma * q_next

        # update critcs
        # [1] <- ([num_critics, batch_size] - [1, batch_size])
        critic_losses = (critic(state, action) - q_target[None,:]).pow(2).mean(dim=1).sum(dim=0)
        # [batch_size, num_critics, action_dim]
        q_gradients = torch.autograd.grad(critic(state, action.requires_grad()), create_graph=True).t()
        q_gradients = q_gradients / q_gradients.norm(p=2,dim=2)
        # [batch_size, num_critics, num_critics]
        q_cosine_similarity_matrix = (q_gradients @ q_gradients.transpose(1,2)) * (1 - torch.eye(config.num_critics, device=config.device))
        # [1]
        diversity_loss = (q_cosine_similarity_matrix.sum(dim=(1,2))).mean() / (config.num_critics - 1)
        critic_loss = critic_losses + config.eta * diversity_loss
        critic_optimizer.zero_grad()
        critic_loss.backward()
        critic_optimizer.step()

        # update actor
        actor_action, actor_action_log_prob = actor(state)
        actor_q_values = critic(state, actor_action)
        actor_loss = (actor_q_values.min().values - config.beta * actor_action_log_prob).mean()
        actor_optimizer.zero_grad()
        actor_loss.backward()
        actor_optimizer.step()

        # update target critic
        with torch.no_grad():
            for target_param, source_param in zip(target_critic.parameters(), critic.parameters()):
                target_param.data.copy_((1 - config.tau) * target_param.data + config.tau * source_param.data)

        # eval and log
        if update_step % config.eval_every == 0:
            # eval
            actor.eval()
            rewards = np.zeros(config.eval_episodes)
            for i in range(config.eval_episodes):
                state = eval_env.reset()
                done = False
                while not done:
                    action, _ = actor(torch.tensor(state, dtype=torch.float32, device=config.device))
                    state, reward, done, _ = eval_env.step(action)
                    rewards[i] += reward
                    # eval_env.render()
            actor.train()

            # log
            wandb.log({
                "step": update_step,
                "critic_loss": critic_loss.item(),
                "actor_loss": actor_loss.item(),
                "eval/mean_reward": np.mean(rewards),
                "eval/std_reward": np.std(rewards),
                "actor/entropy": -actor_action_log_prob.mean().item(),
                "actor/q_value": actor_q_values.mean().item(),
            })

    wandb_run.finish()



if __name__ == '__main__':
    train()
