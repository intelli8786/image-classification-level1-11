import argparse
import json
import multiprocessing
import os
from importlib import import_module
from sklearn.metrics import f1_score
import seaborn as sns
import pandas as pd
from tqdm import tqdm

import numpy as np
import torch
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from torch.optim.lr_scheduler import StepLR
import warnings

from dataset import SubDataset
from loss import create_criterion
import opt
from util import draw_confusion_matrix, seed_everything, increment_path, grid_image


# 경고메세지 끄기
warnings.filterwarnings(action='ignore')


def rand_bbox(size, lam):  # size : [Batch_size, Channel, Width, Height]
    W = size[2]
    H = size[3]
    cut_rat = np.sqrt(1. - lam)  # 패치 크기 비율
    cut_w = np.int(W * cut_rat)
    cut_h = np.int(H * cut_rat)

    # 패치의 중앙 좌표 값 cx, cy
    cx = np.random.randint(W)
    cy = np.random.randint(H)

    # 패치 모서리 좌표 값
    bbx1 = 0
    bby1 = np.clip(cy - cut_h // 2, 0, H)
    bbx2 = W
    bby2 = np.clip(cy + cut_h // 2, 0, H)

    return bbx1, bby1, bbx2, bby2


def train(data_dir, model_dir, args):
    seed_everything(args.seed)

    save_dir = increment_path(os.path.join(model_dir, args.name))

    # -- settings
    # config = ConfigParser("./config.json")
    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")


    # -- dataset
    dataset_module = getattr(import_module("dataset"), args.dataset)  # default: BaseAugmentation
    dataset = dataset_module(
        data_dir=data_dir,
    )
    num_classes = dataset.num_classes  # 18

    # -- augmentation
    transform_module = getattr(import_module("dataset"), args.augmentation)  # default: BaseAugmentation
    transform = transform_module(
        mean=dataset.mean,
        std=dataset.std,
    )

    val_transform_module = getattr(import_module("dataset"), args.val_augmentation)
    val_transform = val_transform_module(
        mean=dataset.mean,
        std=dataset.std,
    )
    # dataset.set_transform(transform)

    # -- data_loader
    train_set, val_set = dataset.split_dataset()
    train_set = SubDataset(train_set, transform=transform)
    val_set = SubDataset(val_set, transform=val_transform)

    train_loader = DataLoader(
        train_set,
        batch_size=args.batch_size,
        num_workers=multiprocessing.cpu_count()//2,
        shuffle=True,
        # pin_memory=use_cuda,
        # drop_last=True,
    )

    val_loader = DataLoader(
        val_set,
        batch_size=args.valid_batch_size,
        num_workers=multiprocessing.cpu_count()//2,
        shuffle=False,
        # pin_memory=use_cuda,
        # drop_last=True,
    )


    # -- model
    model_module = getattr(import_module("model"), args.model)  # default: BaseModel
    model = model_module(
        model_name=args.model_name,
        pretrained=args.pretrained,
        num_classes=num_classes
    )

    # load saved model
    option = args.option
    if option["load"]:
        model.load_state_dict(torch.load(option["path"]))
        model.eval()

    model = model.to(device)
    model = torch.nn.DataParallel(model)

    # -- loss & metric
    criterion = create_criterion(args.criterion)  # default: cross_entropy
    base_optimizer = None
    if args.optimizer == "SAM":
        base_optimizer = getattr(import_module("torch.optim"), args.base_optimizer)
        opt_module = getattr(import_module("opt"), args.optimizer)
        optimizer = opt_module(
            filter(lambda p: p.requires_grad, model.parameters()),
            base_optimizer=base_optimizer,
            lr=args.lr,
            weight_decay=5e-4
        )
    else:
        opt_module = getattr(import_module("torch.optim"), args.optimizer)  # default: SGD
        optimizer = opt_module(
            filter(lambda p: p.requires_grad, model.parameters()),
            lr=args.lr,
            weight_decay=5e-4
        )
    scheduler = getattr(import_module("torch.optim.lr_scheduler"), "StepLR")(optimizer, args.lr_decay_step, gamma=0.5)
    # scheduler = StepLR(optimizer, args.lr_decay_step, gamma=0.5)

    # -- logging
    logger = SummaryWriter(log_dir=os.path.join(save_dir, "tensorboard"))
    with open(os.path.join(save_dir, 'config.json'), 'w', encoding='utf-8') as f:
        json.dump(vars(args), f, ensure_ascii=False, indent=4)

    best_val_loss = np.inf
    best_f1_score = 0

    for epoch in tqdm(range(args.epochs)):
        # train loop
        model.train()

        train_loss = 0

        for idx, train_batch in enumerate(train_loader):
            inputs, labels = train_batch
            inputs = inputs.to(device)
            labels = labels.to(device)

            optimizer.zero_grad()
            r = np.random.rand(1)

            def closure():
                if args.beta > 0 and r < args.cutmix_prob: # cutmix가 실행된 경우
                    lam = np.random.beta(args.beta, args.beta)
                    rand_index = torch.randperm(inputs.size()[0]).to(device)
                    target_a = labels
                    target_b = labels[rand_index]
                    bbx1, bby1, bbx2, bby2 = rand_bbox(inputs.size(), lam)
                    inputs[:, :, bbx1:bbx2, bby1:bby2] = inputs[rand_index, :, bbx1:bbx2, bby1:bby2]
                    lam = 1 - ((bbx2 - bbx1) * (bby2 - bby1) / (inputs.size()[-1] * inputs.size()[-2]))
                    outs = model(inputs)
                    # 패치 이미지와 원본 이미지의 비율에 맞게
                    loss = criterion(outs, target_a) * lam + criterion(outs, target_b) * (1. - lam)
                else:
                    outs = model(inputs)
                    loss = criterion(outs, labels)
                return loss

            loss = closure()
            loss.backward()

            if isinstance(optimizer, opt.SAM):
                optimizer.first_step(zero_grad=True)
                loss = closure()
                loss.backward()
                optimizer.second_step(zero_grad=True)
            else:
                optimizer.step()
            train_loss += loss.item()

        train_loss = train_loss / len(train_loader)

        scheduler.step()

        # val loop
        with torch.no_grad():
            model.eval()
            val_loss_items = []
            val_acc_items = []
            figure = None

            epoch_f1 = 0
            y_pred = np.array([])
            y_true = np.array([])

            for val_batch in val_loader:
                inputs, labels = val_batch
                inputs = inputs.to(device)
                labels = labels.to(device)

                outs = model(inputs)
                preds = torch.argmax(outs, dim=-1)

                epoch_f1 += f1_score(labels.cpu().numpy(), preds.cpu().numpy(), average='macro')

                loss_item = criterion(outs, labels).item()
                acc_item = (labels == preds).sum().item()
                val_loss_items.append(loss_item)
                val_acc_items.append(acc_item)

                y_pred = np.concatenate([y_pred, preds.cpu().view(-1).numpy()])
                y_true = np.concatenate([y_true, labels.cpu().view(-1).numpy()])
                if figure is None:
                    inputs_np = torch.clone(inputs).detach().cpu().permute(0, 2, 3, 1).numpy()
                    inputs_np = dataset_module.denormalize_image(inputs_np, dataset.mean, dataset.std)
                    figure = grid_image(
                        inputs_np, labels, preds, n=16, shuffle=args.dataset != "MaskSplitByProfileDataset"
                    )

            val_loss = np.sum(val_loss_items) / len(val_loader)
            val_acc = np.sum(val_acc_items) / len(val_set)

            epoch_f1 /= len(val_loader)

            best_val_loss = min(best_val_loss, val_loss)

            if epoch_f1 > best_f1_score:
                torch.save(model.module.state_dict(), f"{save_dir}/best.pth")
                best_f1_score = epoch_f1
                draw_confusion_matrix(y_true, y_pred, save_dir, num_classes)

            torch.save(model.module.state_dict(), f"{save_dir}/last.pth")

            # write log
            with open(os.path.join(save_dir, 'log.log'), 'a', encoding='utf-8') as f:
                f.write(
                    f"Epoch {epoch}, F1_Score: {epoch_f1:.3f}, Val Loss: {val_loss:.5f}, "
                    f"Val Acc: {val_acc:.5f}, Train Loss: {train_loss:.5f}\n"
                )

            logger.add_scalar("Train/loss", train_loss, epoch)
            logger.add_scalar("Val/loss", val_loss, epoch)
            logger.add_scalar("Val/accuracy", val_acc, epoch)
            logger.add_scalar("Val/f1_score", epoch_f1, epoch)
            logger.add_figure("results", figure, epoch)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    torch.cuda.empty_cache()
    import os

    # Data and model checkpoints directories
    parser.add_argument('--seed', type=int, default=0, help='random seed (default: 0)')
    parser.add_argument('--epochs', type=int, default=1, help='number of epochs to train (default: 1)')
    parser.add_argument('--dataset', type=str, default='MaskSplitByProfileDataset', help='dataset augmentation type (default: MaskSplitByProfileDataset)')
    parser.add_argument('--augmentation', type=str, default='BaseAugmentation', help='data augmentation type (default: BaseAugmentation)')
    parser.add_argument('--batch_size', type=int, default=64, help='input batch size for training (default: 64)')
    parser.add_argument('--valid_batch_size', type=int, default=64, help='input batch size for validing (default: 1000)')
    parser.add_argument('--model', type=str, default='BaseModel', help='model type (default: BaseModel)')
    parser.add_argument('--optimizer', type=str, default='SGD', help='optimizer type (default: SGD)')
    parser.add_argument('--lr', type=float, default=1e-3, help='learning rate (default: 1e-3)')
    parser.add_argument('--val_ratio', type=float, default=0.2, help='ratio for validaton (default: 0.2)')
    parser.add_argument('--criterion', type=str, default='cross_entropy', help='criterion type (default: cross_entropy)')
    parser.add_argument('--lr_decay_step', type=int, default=20, help='learning rate scheduler deacy step (default: 20)')
    parser.add_argument('--log_interval', type=int, default=20, help='how many batches to wait before logging training status')
    parser.add_argument('--name', default='exp', help='model save at {SM_MODEL_DIR}/{name}')

    parser.add_argument('--pretrained', default=True, help='use pretrained model')
    parser.add_argument('--model_name', default='vit_base_patch16_224', help="pretrained model name")

    # Container environment
    parser.add_argument('--data_dir', type=str, default=os.environ.get('SM_CHANNEL_TRAIN', '/opt/ml/input/data/train/images'))
    parser.add_argument('--model_dir', type=str, default=os.environ.get('SM_MODEL_DIR', './runs'))

    parser.add_argument('--beta', type=float, default=0, help="args.beta")
    parser.add_argument('--cutmix_prob', type=float, default=0, help="prob of cutmix")
    parser.add_argument('--option', type=dict, default={"load": False, "path": "./best.pth"})
    parser.add_argument('--val_augmentation', type=str, default="ValAugmentation", help='data augmentation type for validation (default: ValAugmentation)')
    parser.add_argument('--base_optimizer', default="None", help="base optimizer when optimizer is SAM")

    args = parser.parse_args()
    print(args)

    data_dir = args.data_dir
    model_dir = args.model_dir

    train(data_dir, model_dir, args)
