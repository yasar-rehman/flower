"""Create and connect the building blocks for your experiments; start the simulation.

It includes processioning the dataset, instantiate strategy, specify how the global
model is going to be evaluated, etc. At the end, this script saves the results.
"""
# these are the basic packages you'll need here
# feel free to remove some if aren't needed

import argparse
from argparse import Namespace
from collections import OrderedDict
from typing import Any, Dict, List, Tuple, Optional
import os
import flwr as fl
import numpy as np
from math import exp
import torch
import torch.nn as nn
# pip install mmcv-full==1.2.4 -f https://download.openmmlab.com/mmcv/dist/cu110/torch1.7.1/index.html
import mmcv
# from mmcv import Config
from mmengine.config import Config
from mmcv.runner.checkpoint import load_state_dict, get_state_dict, save_checkpoint
import re
import ray
import time
import shutil
from flwr.common import parameter
from functools import reduce
from flwr.common import (
    EvaluateIns,
    EvaluateRes,
    FitIns,
    FitRes,
    Parameters,
    Scalar,
    NDArrays,
    ndarrays_to_parameters,   # parameters_to_weights,
    parameters_to_ndarrays,   # weights_to_parameters,
)
# import CtP.tools._init_paths
from strategy import FedVSSL
from client import SslClient


def initial_setup(cid, base_work_dir, rounds, data_dir, num_gpus, partition_dir):
    import utils
    cid_plus_one = str(int(cid) + 1)
    args = Namespace(
        cfg='conf/mmcv_conf/r3d_18_ucf101/pretraining.py',
        checkpoint=None, cid=int(cid), data_dir=data_dir, gpus=num_gpus,
        launcher='none',
        local_rank=0, progress=False, resume_from=None, rounds=rounds, seed=7, validate=False,
        work_dir=base_work_dir + '/client' + cid_plus_one)

    print("Starting client", args.cid)
    cfg = Config.fromfile(args.cfg)
    cfg.data.train.data_source.ann_file = partition_dir + '/client_dist' + cid_plus_one + '.json'

    distributed, logger = utils.set_config_mmcv(args, cfg)

    # load the model
    model = utils.load_model(args, cfg)
    # load the training data
    train_dataset = utils.load_data(args, cfg)

    # since pretraining during FedVSSL we don't need any testing data, we can left it empty.
    test_dataset = " " 

    return args, cfg, distributed, logger, model, train_dataset, test_dataset, utils

def fit_config(rnd: int) -> Dict[str, str]:
    """Return a configuration with global epochs."""
    config = {
        "epoch_global": str(rnd),
    }
    return config

def parse_args():
    parser = argparse.ArgumentParser(description='Running FedVSSL and downstream fine-tuning.')
    parser.add_argument('--pre_train', default=True, type=bool,
                        help='set true for FL pre-training, else for downstream fine-tuning.')

    ### hyper-parameters for FL pre-training ###
    parser.add_argument('--exp_name', default='FedVSSL', type=str, help='experimental name used for this run.')
    parser.add_argument('--data_dir', default='/local/scratch/ucf101', type=str, help='dataset directory.')
    parser.add_argument('--partition_dir', default='/local/scratch/ucf101/UCF_101_dummy', type=str,
                        help='directory for FL partition .json files.')

    # FL settings
    parser.add_argument('--pool_size', default=10, type=int, help='number of dataset partitions (= number of total clients).')
    parser.add_argument('--rounds', default=20, type=int, help='number of FL rounds.')
    parser.add_argument('--num_clients_per_round', default=10, type=int, help='number of clients participating in the training.')

    # ray config
    parser.add_argument('--cpus_per_client', default=2, type=int, help='number of CPUs used for each client.')
    parser.add_argument('--gpus_per_client', default=1, type=int, help='number of GPUs used for each client.')
    parser.add_argument('--include_dashboard', default=False, type=bool, help='number of GPUs used for each client.')

    # FedVSSL
    parser.add_argument('--mix_coeff', default=0.2, type=float, help='hyper-parameter alpha in the paper.')
    parser.add_argument('--swbeta', default=1, type=int, help='hyper-parameter beta in the paper.')

    ### hyper-parameters for downstream fine-tuning ###
    parser.add_argument('--pretrained_model_path', default='/home/data1/round-540-weights.array.npz', type=str,
                        help='FL pre-trained SSL model used for downstream fine-tuning.')

    args = parser.parse_args()

    return args


if __name__ == "__main__":
    args = parse_args()

    if args.pre_train:

        # first the paths needs to be defined otherwise the program may not be able to locate the files of the ctp
        from utils import init_p_paths
        init_p_paths("FedVSSL")

        client_resources = {"num_cpus": args.cpus_per_client, "num_gpus": args.gpus_per_client}
        base_work_dir = 'ucf_' + args.exp_name
        rounds = args.rounds
        data_dir = args.data_dir
        partition_dir = args.partition_dir
        num_gpus = args.gpus_per_client

        def client_fn(cid: str):
            args, cfg, distributed, logger, model, train_dataset, test_dataset, videossl = initial_setup(cid,
                                                                                                         base_work_dir,
                                                                                                         rounds,
                                                                                                         data_dir,
                                                                                                         num_gpus,
                                                                                                         partition_dir)
            return SslClient(model, train_dataset, test_dataset, cfg, args, distributed, logger, videossl)

        # configure the strategy
        strategy = FedVSSL(
            mix_coeff=args.mix_coeff,
            swbeta=args.swbeta,
            fraction_fit=(float(args.num_clients_per_round) / args.pool_size),
            min_fit_clients=args.num_clients_per_round,
            min_available_clients=args.pool_size,
            on_fit_config_fn=fit_config,
        )
         # (optional) specify ray config
        ray_config = {"include_dashboard": args.include_dashboard}

        # start simulation
        hist = fl.simulation.start_simulation(
            client_fn=client_fn,
            num_clients=args.pool_size,
            client_resources=client_resources,
            config=fl.server.ServerConfig(num_rounds=args.rounds),
            strategy=strategy,
            ray_init_args=ray_config,
        )
    else:
        import subprocess
        import os
        import textwrap
        from mmcv.runner import load_state_dict
        import textwrap
        from mmcv.runner import load_state_dict
        import CtP
        from CtP.configs.ctp.r3d_18_kinetics import finetune_ucf101
        from CtP.pyvrl.builder import build_model, build_dataset
        
        # we give an example on how one can perform fine-tuning uisng UCF-101 dataset. 
        cfg_path = "CtP/configs/ctp/r3d_18_kinetics/finetune_ucf101.py" 
        cfg = Config.fromfile(cfg_path)
        cfg.model.backbone['pretrained'] = None
        
        # build a model using the configuration file from Ctp repository
        model = build_model(cfg.model)

        # path to the pretrained model. We provide certain federated pretrained model that can be easily downloaded 
        # from the following link: https://github.com/yasar-rehman/FEDVSSL
        # here we gave an example with FedVSSL (alpha=0, beta=0) checkpoint file
        # The files after federated pretraining are usually saved in .npz format. 
        
        pretrained = args.pretrained_model_path
        
        # conversion of the .npz files to the .pth format. If the files are saved in .npz format
        if pretrained.endswith('.npz'):
            # following changes are made here
            params = np.load(pretrained, allow_pickle=True)
            params = params['arr_0'].item()
            params = parameters_to_ndarrays(params)
            params_dict = zip(model.state_dict().keys(), params)
            state_dict = {
                'state_dict':OrderedDict({k: torch.from_numpy(v) for k, v in params_dict})
            }
            torch.save(state_dict, './model_pretrained.pth')
        
       
    #-----------------------------------------------------------------------------------------------------------------------
    # The cfg_path need to be updated with the following updated configuration contents to be able to load the pretrained model.
    # Instead of executing the blow mentioned code, one can also directly modify the "pretrained" variable by opening the path represented
    # by the config_path variable
    #
        config_content = textwrap.dedent('''\
        _base_ = ['../../recognizers/_base_/model_r3d18.py',
        '../../recognizers/_base_/runtime_ucf101.py']
        work_dir = './output/ctp/r3d_18_kinetics/finetune_ucf101/'
        model = dict(
            backbone=dict(
                pretrained='./model_pretrained.pth',
            ),
        )
       ''').strip("\n")

        with open(cfg_path, 'w') as f:
            f.write(config_content)

        
        process_obj = subprocess.run(["bash", "CtP/tools/dist_train.sh",\
        f"{cfg_path}", "4",\
        f"--work_dir /finetune/ucf101/",
        f"--data_dir /home/data1/data/"])

                 
    #-----------------------------------------------------------------------------------------------------------------------
    # The cfg_path need to be updated with the following updated configuration contents to be able to load the pretrained model.
    # Instead of executing the blow mentioned code, one can also directly modify the "pretrained" variable by opening the path represented
    # by the config_path variable
    #
        config_content_test = textwrap.dedent('''\
        _base_ = ['../../recognizers/_base_/model_r3d18.py',
        '../../recognizers/_base_/runtime_ucf101.py']
        work_dir = './output/ctp/r3d_18_ucf101/finetune_ucf101/'
        model = dict(
            backbone=dict(
            pretrained='/finetune/ucf101/epoch_150.pth',
        ),
        )
       ''').strip("\n")

        cfg_path_test= "CtP/configs/ctp/r3d_18_ucf101/finetune_ucf101.py"
        with open(cfg_path_test, 'w') as f:
            f.write(config_content_test)

        # Evaluating the finetuned model 
        process_obj = subprocess.run(["bash", "CtP/tools/dist_test.sh",\
        f"{cfg_path_test}", "4",\
        f"--work_dir /finetune/ucf101/",
        f"--data_dir /home/data1/data",\
        f"--progress"])



# import hydra
# from omegaconf import DictConfig, OmegaConf


# @hydra.main(config_path="conf", config_name="base", version_base=None)
# def main(cfg: DictConfig) -> None:
#     """Run the baseline.

#     Parameters
#     ----------
#     cfg : DictConfig
#         An omegaconf object that stores the hydra config.
#     """
#     # 1. Print parsed config
#     print(OmegaConf.to_yaml(cfg))

    # 2. Prepare your dataset
    # here you should call a function in datasets.py that returns whatever is needed to:
    # (1) ensure the server can access the dataset used to evaluate your model after
    # aggregation
    # (2) tell each client what dataset partitions they should use (e.g. a this could
    # be a location in the file system, a list of dataloader, a list of ids to extract
    # from a dataset, it's up to you)

    # 3. Define your clients
    # Define a function that returns another function that will be used during
    # simulation to instantiate each individual client
    # client_fn = client.<my_function_that_returns_a_function>()

    # 4. Define your strategy
    # pass all relevant argument (including the global dataset used after aggregation,
    # if needed by your method.)
    # strategy = instantiate(cfg.strategy, <additional arguments if desired>)

    # 5. Start Simulation
    # history = fl.simulation.start_simulation(<arguments for simulation>)

    # 6. Save your results
    # Here you can save the `history` returned by the simulation and include
    # also other buffers, statistics, info needed to be saved in order to later
    # on generate the plots you provide in the README.md. You can for instance
    # access elements that belong to the strategy for example:
    # data = strategy.get_my_custom_data() -- assuming you have such method defined.
    # Hydra will generate for you a directory each time you run the code. You
    # can retrieve the path to that directory with this:
#