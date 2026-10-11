"""A tiny, self-contained RL job for the farm.

Learns to balance CartPole with a linear policy improved by random search
(hill climbing). No PyTorch, no GPU needed — just gymnasium + numpy — so it
runs on any worker and is a good end-to-end test of the whole pipeline.

Progress is reported to the farm by printing one JSON line per episode:
    {"episode": {"index": 0, "reward": 23.0, "steps": 23}}

Resuming: the worker sets RESUME_FROM so a job paused by gaming picks back
up instead of starting over. The best policy so far is checkpointed to disk.
"""
import json
import os
import sys

import gymnasium as gym
import numpy as np

CHECKPOINT = "best_policy.npy"


def log(msg: str) -> None:
    print(json.dumps({"log": msg}), flush=True)


def report(index: int, reward: float, steps: int) -> None:
    print(json.dumps({"episode": {"index": index, "reward": reward, "steps": steps}}),
          flush=True)


def run_episode(env, weights, seed=None) -> tuple[float, int]:
    """weights: shape (n_actions, n_obs). Greedy linear policy."""
    obs, _ = env.reset(seed=seed)
    total, steps = 0.0, 0
    done = False
    while not done:
        action = int(np.argmax(weights @ obs))
        obs, reward, terminated, truncated, _ = env.step(action)
        total += reward
        steps += 1
        done = terminated or truncated
    return total, steps


def fitness(env, weights, rng, rollouts=3) -> tuple[float, int]:
    """Average several rollouts so a lucky/unlucky run doesn't fool us."""
    totals, steps = [], []
    for _ in range(rollouts):
        t, s = run_episode(env, weights, seed=int(rng.integers(1 << 30)))
        totals.append(t)
        steps.append(s)
    return float(np.mean(totals)), int(np.mean(steps))


def main() -> None:
    episodes = 200
    if "--episodes" in sys.argv:
        episodes = int(sys.argv[sys.argv.index("--episodes") + 1])
    resume_from = int(os.environ.get("RESUME_FROM", "0"))

    env = gym.make("CartPole-v1")
    n_obs = env.observation_space.shape[0]
    n_actions = env.action_space.n
    rng = np.random.default_rng(0)

    # Load the best policy so far if we're resuming a paused job.
    if resume_from > 0 and os.path.exists(CHECKPOINT):
        best = np.load(CHECKPOINT)
        best_reward, _ = fitness(env, best, rng)
        log(f"resuming from episode {resume_from}, best reward so far {best_reward:.0f}")
    else:
        best = rng.normal(0, 0.1, size=(n_actions, n_obs))
        best_reward, _ = fitness(env, best, rng)

    for i in range(resume_from, episodes):
        # Explore boldly while weak, refine as we approach a full balance (500).
        scale = 0.1 + 0.4 * (1.0 - min(best_reward, 500.0) / 500.0)
        candidate = best + rng.normal(0, scale, size=best.shape)
        reward, steps = fitness(env, candidate, rng)
        report(i, reward, steps)

        if reward > best_reward:
            best_reward, best = reward, candidate
            np.save(CHECKPOINT, best)
        if i % 20 == 0:
            log(f"episode {i}: best reward {best_reward:.0f}/500")

    log(f"finished. best reward {best_reward:.0f}/500")
    env.close()


if __name__ == "__main__":
    main()
