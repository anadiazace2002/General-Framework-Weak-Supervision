# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

import argparse
import os

# changed the line below
os.environ['TF_XLA_FLAGS'] = '--tf_xla_enable_xla_devices=false'

import random
import warnings
warnings.filterwarnings("ignore", category=UserWarning)
import numpy as np
import torch
# torch.autograd.set_detect_anomaly(True)
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.distributed as dist
import torch.multiprocessing as mp
from torch.nn.parallel import DistributedDataParallel as DDP


from src.algorithms import name2alg
from src.core.utils import str2bool, get_logger, get_port, send_model_cuda, count_parameters, over_write_args_from_file, TBLog


def get_config():
    parser = argparse.ArgumentParser(description='Learning with Imprecise Labels - An EM Framework')

    '''
    Saving & loading of the model.
    '''
    parser.add_argument('--save_dir', type=str, default='./saved_models')
    parser.add_argument('-sn', '--save_name', type=str, default='semisup')
    parser.add_argument('--resume', action='store_true')
    parser.add_argument('--load_path', type=str)
    parser.add_argument('-o', '--overwrite', action='store_true', default=True)
    parser.add_argument('--use_tensorboard', action='store_true', help='Use tensorboard to plot and save curves')
    parser.add_argument('--use_wandb', action='store_true', help='Use wandb to plot and save curves')

    '''
    Training Configuration of FixMatch
    '''
    parser.add_argument('--epoch', type=int, default=1)
    parser.add_argument('--num_train_iter', type=int, default=20,
                        help='total number of training iterations')
    parser.add_argument('--num_warmup_iter', type=int, default=0,
                        help='cosine linear warmup iterations')
    parser.add_argument('--num_eval_iter', type=int, default=10,
                        help='evaluation frequency')
    parser.add_argument('--num_log_iter', type=int, default=5,
                        help='logging frequencu')
    parser.add_argument('--batch_size', type=int, default=8)
    parser.add_argument('--eval_batch_size', type=int, default=16,
                        help='batch size of evaluation data loader (it does not affect the accuracy)')
    parser.add_argument('--ema_m', type=float, default=0.999, help='ema momentum for eval_model')

    '''
    Optimizer configurations
    '''
    parser.add_argument('--optim', type=str, default='SGD')
    parser.add_argument('--lr', type=float, default=3e-2)
    parser.add_argument('--momentum', type=float, default=0.9)
    parser.add_argument('--weight_decay', type=float, default=5e-4)
    parser.add_argument('--layer_decay', type=float, default=1.0, help='layer-wise learning rate decay, default to 1.0 which means no layer decay')

    '''
    Backbone Net Configurations
    '''
    # NOTE: change back
    # parser.add_argument('--net', type=str, default='wrn_28_2')
    parser.add_argument('--net', type=str, default='lenet5')
    parser.add_argument('--net_from_name', type=str2bool, default=False)
    parser.add_argument('--use_pretrain', default=False, type=str2bool)
    parser.add_argument('--pretrain_path', default='', type=str)

    '''
    Algorithms Configurations
    '''  

    ## core algorithm setting
    # NOTE: change back
    # parser.add_argument('-alg', '--algorithm', type=str, default='semisup', help='imprecise label configuration')
    parser.add_argument('-alg', '--algorithm', type=str, default='multi_ins', help='imprecise label configuration')
    parser.add_argument('--amp', type=str2bool, default=False, help='use mixed precision training or not')
    parser.add_argument('--clip_grad', type=float, default=0)

    '''
    Data Configurations
    '''

    ## standard setting configurations
    parser.add_argument('--data_dir', type=str, default='./data')
    # NOTE: change back
    # parser.add_argument('-ds', '--dataset', type=str, default='cifar10')
    parser.add_argument('-ds', '--dataset', type=str, default='mnist')
    parser.add_argument('-nc', '--num_classes', type=int, default=10)
    parser.add_argument('--num_workers', type=int, default=1)
    parser.add_argument('--strong_aug', type=str2bool, default=False)
    
    ## cv dataset arguments
    parser.add_argument('--img_size', type=int, default=32)
    parser.add_argument('--crop_ratio', type=float, default=0.875)

    ## nlp dataset arguments 
    parser.add_argument('--max_length', type=int, default=512)

    ## speech dataset algorithms
    parser.add_argument('--max_length_seconds', type=float, default=4.0)
    parser.add_argument('--sample_rate', type=int, default=16000)

    '''
    multi-GPUs & Distrbitued Training
    '''

    ## args for distributed training (from https://github.com/pytorch/examples/blob/master/imagenet/main.py)
    parser.add_argument('--world-size', default=1, type=int,
                        help='number of nodes for distributed training')
    parser.add_argument('--rank', default=0, type=int,
                        help='**node rank** for distributed training')
    parser.add_argument('-du', '--dist-url', default='tcp://127.0.0.1:11111', type=str,
                        help='url used to set up distributed training')
    parser.add_argument('--dist-backend', default='nccl', type=str,
                        help='distributed backend')
    parser.add_argument('--seed', default=1, type=int,
                        help='seed for initializing training. ')
    parser.add_argument('--gpu', default=0, type=int,
                        help='GPU id to use.')
    parser.add_argument('--multiprocessing-distributed', type=str2bool, default=False,
                        help='Use multi-processing distributed training to launch '
                             'N processes per node, which has N GPUs. This is the '
                             'fastest way to use PyTorch for either single node or '
                             'multi node data parallel training')
    
    
    # config file
    parser.add_argument('--c', type=str, default='config/partial_noisy_ulb/classic_cv/imp_partial_noisy_ulb_cifar10_lb50000_n0.1_p0.1_42.yaml')

    # add algorithm specific parameters
    args = parser.parse_args()
    over_write_args_from_file(args, args.c)
    for argument in name2alg[args.algorithm].get_argument():
        parser.add_argument(argument.name, type=argument.type, default=argument.default, help=argument.help, *argument.args, **argument.kwargs)

    args = parser.parse_args()
    # changed the line below
    print(f"Configuración YAML: {args.c}")
    over_write_args_from_file(args, args.c)
    return args

def main(args):
    '''
    For (Distributed)DataParallelism,
    main(args) spawn each process (main_worker) to each GPU.
    '''
    print("entra_main")  # Comprobación inicial
    args.multiprocessing_distributed = False

    save_path = os.path.join(args.save_dir, args.save_name)
    print(f"save_path: {save_path}")  # Comprobación del path de guardado
    
    if os.path.exists(save_path) and args.overwrite and args.resume == False:
        import shutil
        shutil.rmtree(save_path)
        print(f"Removed existing save directory: {save_path}")
    
    if os.path.exists(save_path) and not args.overwrite:
        raise Exception(f'Already existing model: {save_path}')
    
    if args.resume:
        if args.load_path is None:
            raise Exception('Resume of training requires --load_path in the args')
        if os.path.abspath(save_path) == os.path.abspath(args.load_path) and not args.overwrite:
            raise Exception('Saving & Loading paths are same. If you want over-write, give --overwrite in the argument.')
        print("Resuming training from load_path: ", args.load_path)

    if args.seed is not None:
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')
        print(f"Random seed set to: {args.seed}")
    
    if args.gpu == 'None':
        args.gpu = 0  # changed the line 
    print(f"GPU selected: {args.gpu}")  # Comprobación del GPU
    
    if args.gpu is not None:
        warnings.warn('You have chosen a specific GPU. This will completely '
                      'disable data parallelism.')
        print("Warning: GPU selected disables data parallelism.")

    if args.dist_url == "env://" and args.world_size == -1:
        args.world_size = int(os.environ["WORLD_SIZE"])
    print(f"args.world_size: {args.world_size}")  # Comprobación de world_size

    # distributed: true if manually selected or if world_size > 1
    args.distributed = args.world_size > 1 # or args.multiprocessing_distributed
    ngpus_per_node = 1 # torch.cuda.device_count()  # number of gpus of each node
    print(f"Number of GPUs in the node: {ngpus_per_node}")  # Comprobación de la cantidad de GPUs

    if args.multiprocessing_distributed:
        # now, args.world_size means num of total processes in all nodes
        args.world_size = ngpus_per_node * args.world_size
        print(f"Distributed setup: world_size = {args.world_size}")
    else:
        args.world_size = 1  # changed
        print("Single GPU setup, world_size set to 1.")
        main_worker(args.gpu, ngpus_per_node, args)

def main_worker(gpu, ngpus_per_node, args):
    '''
    main_worker is conducted on each GPU.
    '''
    print(f"Entering main_worker for GPU {gpu}")
    
    # Set the CUDA_VISIBLE_DEVICES to the GPU assigned to this worker
    os.environ['CUDA_VISIBLE_DEVICES'] = str(gpu)

    # Random seed setup
    random.seed(args.seed)
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    cudnn.deterministic = False
    cudnn.benchmark = False
    print("Random seeds and CUDA setup done.")
    
    # SET UP FOR DISTRIBUTED TRAINING
    if args.distributed:
        print("Setting up distributed training...")
        if args.dist_url == "env://" and args.rank == -1:
            args.rank = int(os.environ["RANK"])
        if args.multiprocessing_distributed:
            args.rank = args.rank * ngpus_per_node + gpu  # compute global rank
        print(f"Distributed rank for this process: {args.rank}")

        # set distributed group:
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)

    # SET save_path and logger
    save_path = os.path.join(args.save_dir, args.save_name)
    print(f"Final save_path: {save_path}")
    
    logger_level = "WARNING"
    tb_log = None
    if args.rank % ngpus_per_node == 0:
        tb_log = TBLog(save_path, 'tensorboard', use_tensorboard=args.use_tensorboard)
        logger_level = "INFO"

    logger = get_logger(args.save_name, save_path, logger_level)
    logger.info(f"Use GPU: {args.gpu} for training")
    
    # optimizer, scheduler, datasets, dataloaders with be set in algorithms
    model = name2alg[args.algorithm](args, tb_log, logger)
    logger.info(f'Number of Trainable Params: {count_parameters(model.model)}')
    
    def send_model_cuda(args, model, clip_batch=True):
        if args.gpu is not None:
            model = model.to(f'cuda:{args.gpu}')
        else:
            model = model.to('cuda')
        return model
        
    # SET Devices for (Distributed) DataParallel
    model.model = send_model_cuda(args, model.model)
    if model.ema_model is not None:
        model.ema_model = send_model_cuda(args, model.ema_model, clip_batch=False)
    logger.info(f"Arguments: {model.args}")
    
    '''
    model.model = DDP(model.model, device_ids=[gpu], find_unused_parameters=False)
    print("Model wrapped in DDP.")
    '''

    if args.resume:
        if args.load_path is None:
            raise Exception("Resume requires --load_path.")
        if os.path.exists(args.load_path):
            try:
                model.load_model(args.load_path)
                print(f"Model loaded from {args.load_path}")
            except Exception as e:
                logger.warning(f"Failed to resume model from {args.load_path}: {e}")
                args.resume = False
        else:
            logger.warning(f"Load path {args.load_path} does not exist. Starting from scratch.")

    # START TRAINING of FixMatch
    logger.info("Model training")
    model.train()

    # print validation (and test results)
    for key, item in model.results_dict.items():
        logger.info(f"Model result - {key} : {item}")
        print(f"Model result - {key} : {item}")  # Comprobación de los resultados

    logger.warning(f"GPU {args.rank} training is FINISHED")
    print(f"GPU {args.rank} training is FINISHED")

if __name__ == "__main__":
    args = get_config()
    port = get_port()
    args.dist_url = "tcp://127.0.0.1:" + str(port)
    main(args)
