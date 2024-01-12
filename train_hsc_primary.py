# Training script for decam data
try:
    # ignore ShapelyDeprecationWarning from fvcore
    import warnings

    from shapely.errors import ShapelyDeprecationWarning

    warnings.filterwarnings("ignore", category=ShapelyDeprecationWarning)

except:
    pass
warnings.filterwarnings("ignore", category=RuntimeWarning)


# Some basic setup:
# Setup detectron2 logger
from detectron2.utils.logger import setup_logger

setup_logger()

import copy
import gc
import os
import time

import detectron2.data as data
import detectron2.data.transforms as T
import detectron2.modeling as modeler
import detectron2.solver as solver
import detectron2.utils.comm as comm
import imgaug.augmenters as iaa

# from google.colab.patches import cv2_imshow

# import some common libraries
import numpy as np
import torch

# import some common detectron2 utilities
from detectron2 import model_zoo
from detectron2.config import get_cfg
from detectron2.data import detection_utils as utils
from detectron2.engine import (
    launch,
)

from astrodet import astrodet as toolkit
from astrodet import detectron as detectron_addons

# Prettify the plotting
from astrodet.astrodet import set_mpl_style, test_mapper_cls, train_mapper_cls

set_mpl_style()




from astrodet.detectron import _transform_to_aug

from deepdisc.data_format.file_io import get_data_from_json
from deepdisc.data_format.register_data import register_data_set
from deepdisc.utils.parse_arguments import dtype_from_args, make_training_arg_parser


# ### Augment Data


def gaussblur(image):
    aug = iaa.GaussianBlur(sigma=(0.0, np.random.random_sample() * 4 + 2))
    return aug.augment_image(image)


def addelementwise16(image):
    aug = iaa.AddElementwise((-3276, 3276))
    return aug.augment_image(image)


def addelementwise8(image):
    aug = iaa.AddElementwise((-25, 25))
    return aug.augment_image(image)


def addelementwise(image):
    aug = iaa.AddElementwise((-image.max() * 0.1, image.max() * 0.1))
    return aug.augment_image(image)


def centercrop(image):
    h, w = image.shape[:2]
    hc = (h - h // 2) // 2
    wc = (w - w // 2) // 2
    image = image[hc : hc + h // 2, wc : wc + w // 2]
    return image


def main(tl, train_head, args):
    """
    This function instantiates the model, dataloaders, and trainer
    and trains the model for 50 epochs following the schedule form Burke et al 2019

    Parameters
    ----------
    tl: int
        Size of training data set
    train_head: bool
        whether or not to train only the head layers
    args: keyword args
        arguments taken from the command line


    """
    # Hack if you get SSL certificate error
    import ssl

    ssl._create_default_https_context = ssl._create_unverified_context

    output_dir = args.output_dir
    output_name = args.run_name
    cfgfile = args.cfgfile
    dirpath = args.data_dir  # Path to dataset
    scheme = args.scheme
    alphas = args.alphas
    dtype = dtype_from_args(args.dtype)

    # ### Prepare For Training
    # Training logic:
    # To replicate 2019 methodology, need to
    # 1) run intially with backbone frozen (freeze_at=4) for 15 epochs
    # 2) unfreeze and run for [25,35,50] epochs with lr decaying by 0.1x each time
    dataset_names = ["train", "test", "val"]

    trainfile = dirpath + dataset_names[0] + "_sample_new.json"
    testfile = dirpath + dataset_names[1] + "_sample_new.json"
    valfile = dirpath + dataset_names[2] + "_sample_new.json"

    if scheme == 1 or scheme == 3:
        classes = ["star", "galaxy", "bad_fit", "unknown"]
    elif scheme == 2 or scheme == -1:
        # classes =["star", "galaxy","bad_fit"]
        classes = ["star", "galaxy"]
    numclasses = len(classes)

    # Register the data sets and get the metadata.
    astrotrain_metadata = register_data_set("astro_train", trainfile, thing_classes=classes)
    astroval_metadata = register_data_set("astro_val", testfile, thing_classes=classes)

    cfg = get_cfg()
    cfg.merge_from_file(model_zoo.get_config_file(cfgfile))  # Get model structure
    cfg.DATASETS.TRAIN = "astro_train"  # Register Metadata
    cfg.DATASETS.TEST = "astro_val"  # Config calls this TEST, but it should be the val dataset
    cfg.DATALOADER.NUM_WORKERS = 1
    cfg.MODEL.ROI_HEADS.BATCH_SIZE_PER_IMAGE = 512
    cfg.MODEL.ROI_HEADS.NUM_CLASSES = numclasses
    cfg.MODEL.RPN.BATCH_SIZE_PER_IMAGE = 512

    if args.norm == "astrolupton":
        cfg.MODEL.PIXEL_MEAN = [13.49794151, 9.11051305, 5.42995532]
    elif args.norm == "astroluptonhc":
        cfg.MODEL.PIXEL_MEAN = [37.92064421, 25.80468069, 14.03756261]
    elif args.norm == "zscore":
        cfg.MODEL.PIXEL_MEAN = [1.02938894, -11.65404583, -26.35697284]

    cfg.INPUT.MIN_SIZE_TRAIN = 500
    cfg.INPUT.MAX_SIZE_TRAIN = 525

    cfg.MODEL.ANCHOR_GENERATOR.SIZES = [[8, 16, 32, 64, 128]]
    cfg.SOLVER.IMS_PER_BATCH = (
        8  # this is images per iteration. 1 epoch is len(images)/(ims_per_batch iterations)
    )

    cfg.OUTPUT_DIR = output_dir
    cfg.TEST.DETECTIONS_PER_IMAGE = 1000

    cfg.SOLVER.CLIP_GRADIENTS.ENABLED = True
    # Type of gradient clipping, currently 2 values are supported:
    # - "value": the absolute values of elements of each gradients are clipped
    # - "norm": the norm of the gradient for each parameter is clipped thus
    #   affecting all elements in the parameter
    cfg.SOLVER.CLIP_GRADIENTS.CLIP_TYPE = "norm"
    # Maximum absolute value used for clipping gradients
    # Floating point number p for L-p norm to be used with the "norm"
    # gradient clipping type; for L-inf, please specify .inf
    cfg.SOLVER.CLIP_GRADIENTS.NORM_TYPE = 5.0

    # iterations for 15,25,35,50 epochs
    epoch = int(tl / cfg.SOLVER.IMS_PER_BATCH)
    e1 = epoch * 15
    e2 = epoch * 10
    e3 = epoch * 20
    efinal = epoch * 35

    val_per = epoch

    cfg.MODEL.RPN.POST_NMS_TOPK_TRAIN = 6000
    cfg.MODEL.ROI_HEADS.POSITIVE_FRACTION = 0.33

    if train_head:
        # Step 1)

        cfg.MODEL.BACKBONE.FREEZE_AT = 4  # Initial re-training of the head layers (i.e. freeze the backbone)
        if args.from_scratch:
            cfg.MODEL.BACKBONE.FREEZE_AT = 0
        cfg.SOLVER.BASE_LR = 0.001
        cfg.SOLVER.STEPS = []  # do not decay learning rate for retraining
        cfg.SOLVER.LR_SCHEDULER_NAME = "WarmupMultiStepLR"
        cfg.SOLVER.WARMUP_ITERS = 0
        cfg.SOLVER.MAX_ITER = e1  # for DefaultTrainer

        # init_coco_weights = True # Start training from MS COCO weights

        if not args.from_scratch:
            cfg.MODEL.WEIGHTS = model_zoo.get_checkpoint_url(cfgfile)  # Initialize from MS COCO
        # else:
        #    cfg.MODEL.WEIGHTS = os.path.join(output_dir, 'model_temp.pth')  # Initialize from a local weights

        os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)
        model = modeler.build_model(cfg)
        optimizer = solver.build_optimizer(cfg, model)

        _train_mapper = train_mapper_cls(
            normalize=args.norm,
            ceil_percentile=args.cp,
            dtype=dtype,
            A=args.A,
            stretch=args.stretch,
            Q=args.Q,
            do_norm=args.do_norm,
        )
        _test_mapper = test_mapper_cls(
            normalize=args.norm,
            ceil_percentile=args.cp,
            dtype=dtype,
            A=args.A,
            stretch=args.stretch,
            Q=args.Q,
            do_norm=args.do_norm,
        )

        loader = data.build_detection_train_loader(cfg, mapper=_train_mapper)
        test_loader = data.build_detection_test_loader(cfg, cfg.DATASETS.TEST, mapper=_test_mapper)

        saveHook = detectron_addons.SaveHook()
        saveHook.set_output_name(output_name)
        schedulerHook = detectron_addons.CustomLRScheduler(optimizer=optimizer)
        lossHook = detectron_addons.LossEvalHook(val_per, model, test_loader)
        hookList = [lossHook, schedulerHook, saveHook]
        # hookList = [schedulerHook,saveHook]

        trainer = toolkit.NewAstroTrainer(model, loader, optimizer, cfg)
        trainer.register_hooks(hookList)
        trainer.set_period(int(epoch / 2))  # print loss every n iterations
        trainer.train(0, e1)
        # trainer.set_period(5)
        # trainer.train(0,20)
        if comm.is_main_process():
            np.save(output_dir + output_name + "_losses", trainer.lossList)
            np.save(output_dir + output_name + "_val_losses", trainer.vallossList)
        return

    else:
        # Step 2)

        cfg.MODEL.BACKBONE.FREEZE_AT = 0  # unfreeze all backbone layers
        cfg.SOLVER.BASE_LR = 0.0001
        cfg.SOLVER.STEPS = [e2, e3]  # decay learning rate
        cfg.SOLVER.LR_SCHEDULER_NAME = "WarmupMultiStepLR"
        cfg.SOLVER.WARMUP_ITERS = 0
        cfg.SOLVER.MAX_ITER = efinal  # for LR scheduling
        cfg.MODEL.WEIGHTS = os.path.join(output_dir, output_name + ".pth")  # Initialize from a local weights

        _train_mapper = train_mapper_cls(
            normalize=args.norm,
            ceil_percentile=args.cp,
            dtype=dtype,
            A=args.A,
            stretch=args.stretch,
            Q=args.Q,
            do_norm=args.do_norm,
        )
        _test_mapper = test_mapper_cls(
            normalize=args.norm,
            ceil_percentile=args.cp,
            dtype=dtype,
            A=args.A,
            stretch=args.stretch,
            Q=args.Q,
            do_norm=args.do_norm,
        )

        model = modeler.build_model(cfg)
        optimizer = solver.build_optimizer(cfg, model)
        loader = data.build_detection_train_loader(cfg, mapper=_train_mapper)
        test_loader = data.build_detection_test_loader(cfg, cfg.DATASETS.TEST, mapper=_test_mapper)

        saveHook = detectron_addons.SaveHook()
        saveHook.set_output_name(output_name)
        schedulerHook = detectron_addons.CustomLRScheduler(optimizer=optimizer)
        lossHook = detectron_addons.LossEvalHook(val_per, model, test_loader)
        hookList = [lossHook, schedulerHook, saveHook]
        # hookList = [schedulerHook,saveHook]

        trainer = toolkit.NewAstroTrainer(model, loader, optimizer, cfg)
        trainer.register_hooks(hookList)
        trainer.set_period(int(epoch / 2))  # print loss every n iterations
        trainer.train(0, efinal)
        # trainer.set_period(5) # print loss every n iterations
        # trainer.train(0,20)

        if comm.is_main_process():
            losses = np.load(output_dir + output_name + "_losses.npy")
            losses = np.concatenate((losses, trainer.lossList))
            np.save(output_dir + output_name + "_losses", losses)

            vallosses = np.load(output_dir + output_name + "_val_losses.npy")
            vallosses = np.concatenate((vallosses, trainer.vallossList))
            np.save(output_dir + output_name + "_val_losses", vallosses)

        return


if __name__ == "__main__":
    """
    Runs the training of the head layers for 15 epochs
    then runs the training of the full model for an additional 35 epochs
    """
    args = make_training_arg_parser().parse_args()
    print("Command Line Args:", args)

    dirpath = "/home/shared/hsc/HSC/HSC_DR3/data/"  # Path to dataset

    dataset_names = ["train", "test", "val"]

    traind = get_data_from_json(os.path.join(dirpath, dataset_names[0]) + "_sample.json")
    testd = get_data_from_json(os.path.join(dirpath, dataset_names[2]) + "_sample.json")

    # number of total samples
    print("# of train sample: ", len(traind))
    print("# of val sample: ", len(testd))

    tl = len(traind)
    del traind, testd
    gc.collect()

    print("Training head layers")
    train_head = True
    t0 = time.time()
    launch(
        main,
        args.num_gpus,
        num_machines=args.num_machines,
        machine_rank=args.machine_rank,
        dist_url=args.dist_url,
        args=(
            tl,
            dataset_names,
            train_head,
            args,
        ),
    )

    print("Training full model")
    train_head = False
    launch(
        main,
        args.num_gpus,
        num_machines=args.num_machines,
        machine_rank=args.machine_rank,
        dist_url=args.dist_url,
        args=(
            tl,
            dataset_names,
            train_head,
            args,
        ),
    )
    print(f"Took {time.time()-t0} seconds")
