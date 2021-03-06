#!/usr/bin/env python3
from tensorboardX import SummaryWriter
import argparse
import time
import os
from utils import epoch_info
import yaml
from training import (
    Optimizer,
    Scheduler,
    EarlyStopper,
    ModelHandler,
    AdvRunner,
    AdvSegLearner,
    DisLearner
)
from MIDP import DataLoader, DataGenerator, Reverter
from flows import MetricFlow
import json
from tqdm import tqdm
from utils import get_tty_columns
import numpy as np
import torch


parser = argparse.ArgumentParser()
parser.add_argument(
    '--config',
    required=True,
    help='training config'
)
parser.add_argument(
    '--checkpoint',
    default=None,
    help='pretrained model checkpoint'
)
parser.add_argument(
    '--checkpoint-dis',
    default=None,
    help='pretrained model checkpoint'
)
parser.add_argument(
    '--checkpoint-dir',
    default='_ckpts',
    help='saved model checkpoints'
)
parser.add_argument(
    '--log-dir',
    default='_logs',
    help='training logs'
)
parser.add_argument(
    '--pause-ckpt',
    help='save model checkpoint if paused'
)
args = parser.parse_args()


# load config
with open(args.config) as f:
    config = yaml.safe_load(f)
generator_config = config['generator']
stages = generator_config.keys()
with open(config['data']) as f:
    data_config = yaml.safe_load(f)
data_list = data_config['list']
loader_config = data_config['loader']

# FIXME
if 'train_dis' in config:
    train_dis = config['train_dis']
else:
    train_dis = False

if 'unlabeled' in config:
    unlabeled = config['unlabeled']
else:
    unlabeled = False

if 'start_adv' in config:
    start_adv = int(config['start_adv'])
else:
    start_adv = 1

if train_dis:
    print('Train discriminator only.')
if unlabeled:
    print('Train with unlabeled data.')
if start_adv:
    print('Start adv after %d iterations.' % start_adv)


# - data pipeline
data_gen = dict()
loader_name = loader_config.pop('name')
ROIs = None
for stage in stages:
    data_loader = DataLoader(loader_name, **loader_config)
    if data_list[stage] is not None:
        data_loader.set_data_list(data_list[stage])
    data_gen[stage] = DataGenerator(data_loader, generator_config[stage])

    if ROIs is None:
        ROIs = data_loader.ROIs

# FIXME
reverter = Reverter(data_gen['valid'])

# - GPUs
os.environ['CUDA_VISIBLE_DEVICES'] = str(config['gpus'])
torch.backends.cudnn.enabled = True

# - model
model_handlers = {
    'seg': ModelHandler(
        config['models']['seg'],
        checkpoint=args.checkpoint,
    ),
    'dis': ModelHandler(
        config['models']['dis'],
        checkpoint=args.checkpoint_dis,
    ),
}

# - optimizer
optimizers = {
    key: Optimizer(config['optimizers'][key])(model_handlers[key].model)
    for key in ['seg', 'dis']
}

# - scheduler
if 'scheduler' in config:
    scheduler = Scheduler(
        optimizers['seg'],
        **config['scheduler']
    )
else:
    scheduler = None


if args.log_dir is not None:
    logger = SummaryWriter(args.log_dir)
else:
    logger = None

timer = time.time()
start = timer

# FIXME
if 'grad_accumulation' in config:
    grad_accumulation = config['grad_accumulation']
else:
    grad_accumulation = 1
if grad_accumulation > 1:
    print('grad_accumulation:', grad_accumulation)


learners = {
    'seg': AdvSegLearner(
        model=model_handlers['seg'].model,
        meter=MetricFlow(config['meters']['seg']),
        optim=optimizers['seg'],
        discriminator=model_handlers['dis'].model,
    ),
    'dis': DisLearner(
        model=model_handlers['dis'].model,
        meter=MetricFlow(config['meters']['dis']),
        optim=optimizers['dis'],
    ),
}
runner = AdvRunner(
    learners,
    logger=logger,
    start_adv=start_adv,
)

checkpoint_dir = args.checkpoint_dir
if checkpoint_dir:
    os.makedirs(checkpoint_dir, exist_ok=True)

# early stopper
if 'early_stopper' in config:
    early_stopper = EarlyStopper(**config['early_stopper'])
else:
    early_stopper = None

# set proper initial epoch
if model_handlers['seg'].checkpoint is not None:
    init_epoch = model_handlers['seg'].checkpoint['epoch']
else:
    init_epoch = 1

# main running loop
terminated = False
scheduler_metric = None
stage_info = {'train': 'Training', 'valid': 'Validating'}
for epoch in range(init_epoch, init_epoch + config['epochs']):

    if terminated:
        break

    # epoch start
    epoch_info(epoch - 1, init_epoch + config['epochs'] - 1)

    for stage in stages:
        training = True if stage == 'train' else False

        # skip validation stage by validation_frequency
        if stage != 'train' and epoch % config['validation_frequency'] != 0:
            break

        # run on an epoch
        try:
            result_list = runner.run(
                data_gen[stage],
                training=training,
                unlabeled=unlabeled,  # FIXME
                train_dis=train_dis,  # FIXME
                stage=stage_info[stage]
            )

        except KeyboardInterrupt:
            print('save temporary model into %s' % args.pause_ckpt)
            # FIXME
            if not train_dis:
                model_handlers['seg'].save(
                    args.pause_ckpt,
                    additional_info={'epoch': epoch, 'step': runner.step}
                )
            # TODO: improve code
            model_handlers['dis'].save(
                'pause_dis.pt',
                additional_info={'epoch': epoch, 'step': runner.step}
            )
            terminated = True
            break

        # collect results except those revertible ones, e.g., accu, losses
        result = {
            key: np.nanmean(
                np.vstack([result[key] for result in result_list]),
                axis=0
            )
            for key in result_list[0].keys()
            if key not in reverter.revertible
        }

        # summarize the performance
        accu = result.pop('accu')
        accu_dict = {key: val for key, val in zip(ROIs, accu)}
        mean_accu = np.nanmean(accu)
        accu_dict.update({'mean': mean_accu})
        print(', '.join(
            '%s: %.5f' % (key, val)
            for key, val in result.items()
        ))
        print('Accu: ' + ', '.join(
            '%s: %.5f' % (key, val)
            for key, val in accu_dict.items()
        ))

        # record the performance
        if logger is not None:
            for key, val in result.items():
                logger.add_scalar(
                    '%s/epoch/%s' % (stage_info[stage], key),
                    val,
                    epoch
                )
            logger.add_scalar(
                '%s/epoch/mean_accu' % stage_info[stage],
                mean_accu,
                epoch
            )
            logger.add_scalars(
                '%s/epoch/accu' % stage_info[stage],
                accu_dict,
                epoch
            )

        # FIXME
        if train_dis:
            model_handlers['dis'].save(
                file_path=os.path.join(
                    checkpoint_dir,
                    'best-dis.pt'
                ),
                additional_info={'epoch': epoch, 'step': runner.step}
            )
            break

        # do some stuffs depending on validation
        if stage == 'valid':

            # revert the matching dice score to the whole one from batches
            scores = dict()
            progress_bar = tqdm(
                reverter.on_batches(result_list),
                total=len(reverter.data_list),
                dynamic_ncols=True,
                ncols=get_tty_columns(),
                desc='[Data index]'
            )
            for reverted in progress_bar:
                data_idx = reverted['idx']
                scores[data_idx] = reverted['score']
                info = '[%s] ' % data_idx
                info += ', '.join(
                    '%s: %.3f' % (key, val)
                    for key, val in scores[data_idx].items()
                )
                progress_bar.set_description(info)

            # summerize roi score
            roi_scores = {
                roi: np.mean([
                    scores[data_idx][roi] for data_idx in scores
                ])
                for roi in ROIs
            }
            roi_scores.update({
                'mean': np.mean([
                    roi_scores[roi]
                    for roi in ROIs
                ])
            })
            print('Scores: ' + ', '.join(
                '%s: %.5f' % (key, val)
                for key, val in roi_scores.items()
            ))
            if logger is not None:
                logger.add_scalars('roi_scores', roi_scores, epoch)
                file_path = os.path.join(
                    args.log_dir,
                    '%02d-%.5f.json' % (epoch, roi_scores['mean'])
                )
                with open(file_path, 'w') as f:
                    json.dump(scores, f, indent=2)

            # update metric for learning rate scheduler
            if scheduler and scheduler.use_reduce_lr:
                if scheduler.mode == 'min':
                    scheduler_metric = result['loss']
                else:
                    scheduler_metric = roi_scores['mean']

            # TODO: separate early stopping and checkpoint saving
            # check early stopping
            if early_stopper is not None:
                if early_stopper.mode == 'min':
                    early_stopper_metric = result['loss']
                else:
                    early_stopper_metric = roi_scores['mean']
                early_stop, improved = early_stopper.check(early_stopper_metric)

                if early_stop:
                    print('Early stopped.')
                    terminated = True
                    break

                elif improved and checkpoint_dir is not None:
                    model_handlers['seg'].save(
                        file_path=os.path.join(
                            checkpoint_dir,
                            '%02d-%.5f.pt' % (epoch, early_stopper_metric)
                        ),
                        additional_info={'epoch': epoch, 'step': runner.step}
                    )
                    model_handlers['dis'].save(
                        file_path=os.path.join(
                            checkpoint_dir,
                            'dis-%02d-%.5f.pt' % (epoch, early_stopper_metric)
                        ),
                        additional_info={'epoch': epoch, 'step': runner.step}
                    )

    # adjust learning rate by epoch
    if scheduler and not terminated:

        if all((
            scheduler.use_reduce_lr,
            stage == 'valid',
            scheduler_metric is not None
        )):
            scheduler.step(metric=scheduler_metric)
        else:
            scheduler.step()

        if logger:
            logger.add_scalar(
                'scheduler/lr_rate',
                optimizers['dis'].param_groups[0]['lr'],
                epoch
            )
            if scheduler.best is not None:
                logger.add_scalar(
                    'scheduler/best_metric',
                    scheduler.best,
                    epoch
                )

logger.close()
print('Total:', time.time()-start)
print('Finished Training')
