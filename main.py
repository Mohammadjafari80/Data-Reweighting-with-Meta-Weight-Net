import argparse
from collections import Counter

import torch
import torch.nn.functional as F
import torch.optim as optim
from torch.utils.data import WeightedRandomSampler

from model import *
from data import *
from utils import *

from betty.engine import Engine
from betty.problems import ImplicitProblem
from betty.configs import Config, EngineConfig


parser = argparse.ArgumentParser(description="Meta_Weight_Net")
parser.add_argument("--device", type=str, default="cuda")
parser.add_argument("--precision", type=str, default="fp32")
parser.add_argument("--strategy", type=str, default="default")
parser.add_argument("--rollback", action="store_true")
parser.add_argument("--baseline", action="store_true")
parser.add_argument("--retrain", action="store_true")
parser.add_argument("--seed", type=int, default=1)
parser.add_argument("--local_rank", type=int, default=0)
parser.add_argument("--meta_net_hidden_size", type=int, default=100)
parser.add_argument("--meta_net_num_layers", type=int, default=1)

parser.add_argument("--lr", type=float, default=0.1)
parser.add_argument("--momentum", type=float, default=0.9)
parser.add_argument("--dampening", type=float, default=0.0)
parser.add_argument("--nesterov", type=bool, default=False)
parser.add_argument("--weight_decay", type=float, default=5e-4)
parser.add_argument("--meta_lr", type=float, default=1e-5)
parser.add_argument("--meta_weight_decay", type=float, default=0.0)

parser.add_argument("--dataset", type=str, default="cifar10")
parser.add_argument("--subset_indices", type=int, nargs="+", default=None)
parser.add_argument("--num_meta", type=int, default=1000)
parser.add_argument("--imbalanced_factor", type=int, default=None)
parser.add_argument("--corruption_type", type=str, default=None)
parser.add_argument("--corruption_ratio", type=float, default=0.0)
parser.add_argument("--batch_size", type=int, default=100)
parser.add_argument("--max_epoch", type=int, default=120)

parser.add_argument("--meta_interval", type=int, default=1)
parser.add_argument("--paint_interval", type=int, default=20)

args = parser.parse_args()
print(args)
set_seed(args.seed)

sampler = None
resume_idxes = None
resume_labels = None
if args.retrain:
    sample_weight = torch.load("reweight.pt")
    resume_idxes = torch.load("train_index.pt")
    resume_labels = torch.load("train_label.pt")
    sampler = WeightedRandomSampler(sample_weight, len(sample_weight))

(
    train_dataloader,
    meta_dataloader,
    test_dataloader,
    imbalanced_num_list,
) = build_dataloader(
    seed=args.seed,
    dataset=args.dataset,
    num_meta_total=args.num_meta,
    imbalanced_factor=args.imbalanced_factor,
    corruption_type=args.corruption_type,
    corruption_ratio=args.corruption_ratio,
    batch_size=args.batch_size,
    resume_idxes=resume_idxes,
    resume_labels=resume_labels,
    sampler=sampler,
    subset_indices=args.subset_indices,
)

print(Counter(train_dataloader.dataset.targets))


class Outer(ImplicitProblem):
    def forward(self, x):
        return self.module(x)

    def training_step(self, batch):
        inputs, labels = batch
        outputs = self.inner(inputs)
        loss = F.cross_entropy(outputs, labels.long())
        acc = (outputs.argmax(dim=1) == labels.long()).float().mean().item() * 100

        return {"loss": loss, "acc": acc}

    def configure_train_data_loader(self):
        return meta_dataloader

    def configure_module(self):
        meta_net = MLP(
            hidden_size=args.meta_net_hidden_size, num_layers=args.meta_net_num_layers
        )
        return meta_net

    def configure_optimizer(self):
        meta_optimizer = optim.Adam(
            self.module.parameters(),
            lr=args.meta_lr,
            weight_decay=args.meta_weight_decay,
        )
        return meta_optimizer


class Inner(ImplicitProblem):
    def forward(self, x):
        return self.module(x)

    def training_step(self, batch):
        inputs, labels = batch
        outputs = self.forward(inputs)
        if args.baseline or args.retrain:
            return F.cross_entropy(outputs, labels.long())
        loss_vector = F.cross_entropy(outputs, labels.long(), reduction="none")
        loss_vector_reshape = torch.reshape(loss_vector, (-1, 1))
        weight = self.outer(loss_vector_reshape.detach())
        loss = torch.mean(weight * loss_vector_reshape)

        return loss

    def configure_train_data_loader(self):
        return train_dataloader

    def configure_module(self):
        return ResNet32(args.dataset == "cifar10" and 10 or 100)

    def configure_optimizer(self):
        optimizer = optim.SGD(
            self.module.parameters(),
            lr=args.lr,
            momentum=args.momentum,
            dampening=args.dampening,
            weight_decay=args.weight_decay,
            nesterov=args.nesterov,
        )
        return optimizer

    def configure_scheduler(self):
        scheduler = optim.lr_scheduler.MultiStepLR(
            self.optimizer, milestones=[10000, 13000], gamma=0.1
        )
        return scheduler


best_acc = -1


class ReweightingEngine(Engine):
    @torch.no_grad()
    def validation(self):
        correct = 0
        total = 0
        global best_acc
        for x, target in test_dataloader:
            x, target = x.to(args.device), target.to(args.device)
            with torch.no_grad():
                out = self.inner(x)
            correct += (out.argmax(dim=1) == target).sum().item()
            total += x.size(0)
        acc = correct / total * 100
        if best_acc < acc:
            best_acc = acc
        if not args.retrain and not args.baseline:
            torch.save(
                self.inner.state_dict(), f"{args.dataset}/net_{self.global_step}.pt"
            )
            torch.save(
                self.outer.state_dict(),
                f"{args.dataset}/meta_net_{self.global_step}.pt",
            )
        return {"acc": acc, "best_acc": best_acc}


outer_config = Config(type="darts", log_step=100, retain_graph=True)
inner_config = Config(type="darts", unroll_steps=1)
engine_config = EngineConfig(
    train_iters=15000,
    valid_step=500,
    strategy=args.strategy,
    roll_back=args.rollback,
    logger_type="tensorboard",
)
outer = Outer(name="outer", config=outer_config)
inner = Inner(name="inner", config=inner_config)

if args.baseline or args.retrain:
    problems = [inner]
    u2l, l2u = {}, {}
else:
    problems = [outer, inner]
    u2l = {outer: [inner]}
    l2u = {inner: [outer]}
dependencies = {"l2u": l2u, "u2l": u2l}

engine = ReweightingEngine(
    config=engine_config, problems=problems, dependencies=dependencies
)
engine.run()
print(f"IF {args.imbalanced_factor} || Best Acc.: {best_acc}")
