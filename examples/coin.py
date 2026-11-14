"""Learn the bias of a coin; stop and resume with the same model and optimizer.

uv run --extra train etude train examples/coin.py --until updates=10
uv run --extra train etude train examples/coin.py --resume coin-seed-0 --until updates=20
"""

import torch
from pydantic import BaseModel, Field

from etude.train.records import Step
from etude.train.tracking import track


class Config(BaseModel):
    updates: int = Field(default=20, ge=1)
    heads: float = Field(default=0.7, ge=0, le=1)
    lr: float = Field(default=0.1, gt=0)


def train(config: Config) -> None:
    with track(clocks=[Step("updates", "observed coin tosses")], config=config) as run:
        torch.manual_seed(run.seed)
        policy = torch.nn.Linear(1, 1)
        optimizer = torch.optim.SGD(policy.parameters(), lr=config.lr)
        run.resume(policy=policy, optimizer=optimizer)
        for update in run.steps("updates", to=config.updates, every=5):
            toss = (torch.rand(1, 1) < config.heads).float()
            logit = policy(torch.ones(1, 1))
            loss = torch.nn.functional.binary_cross_entropy_with_logits(logit, toss)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
            run.log({"loss": loss.item(), "heads": logit.sigmoid().item()})
            run.save()
            if update % 5 == 0:
                run.checkpoint(policy=policy)
        run.checkpoint(policy=policy)
