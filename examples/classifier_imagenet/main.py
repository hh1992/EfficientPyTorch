from examples import *
import torchvision.models as models
import models.imagenet as imagenet_extra_models
import torch.multiprocessing as mp
import torch.distributed as dist
import random
import torch.backends.cudnn as cudnn
import torch.nn as nn

model_names = sorted(name for name in models.__dict__
                     if name.islower() and not name.startswith("__")
                     and callable(models.__dict__[name]))
model_names.extend(sorted(name for name in imagenet_extra_models.__dict__
                          if name.islower() and not name.startswith("__")
                          and callable(imagenet_extra_models.__dict__[name])))
best_acc1 = 0


def main():
    parser = get_base_parser()
    parser.add_argument('-a', '--arch', metavar='ARCH', default='resnet18',
                        choices=model_names,
                        help='model architecture: ' +
                             ' | '.join(model_names) +
                             ' (default: resnet18)')
    args = parser.parse_args()

    if args.seed is not None:
        random.seed(args.seed)
        torch.manual_seed(args.seed)
        cudnn.deterministic = True
        warnings.warn('You have chosen to seed training. '
                      'This will turn on the CUDNN deterministic setting, '
                      'which can slow down your training considerably! '
                      'You may see unexpected behavior when restarting '
                      'from checkpoints.')

    if args.gpu is not None:
        warnings.warn('You have chosen a specific GPU. This will completely '
                      'disable data parallelism.')

    if args.dist_url == "env://" and args.world_size == -1:
        args.world_size = int(os.environ["WORLD_SIZE"])

    args.distributed = args.world_size > 1 or args.multiprocessing_distributed

    ngpus_per_node = torch.cuda.device_count()
    print('ngpus_per_node: {}'.format(ngpus_per_node))
    if args.multiprocessing_distributed:
        # Since we have ngpus_per_node processes per node, the total world_size
        # needs to be adjusted accordingly
        args.world_size = ngpus_per_node * args.world_size
        # Use torch.multiprocessing.spawn to launch distributed processes: the
        # main_worker process function
        mp.spawn(main_worker, nprocs=ngpus_per_node, args=(ngpus_per_node, args))

    else:
        # Simply call main_worker function
        main_worker(args.gpu, ngpus_per_node, args)


def main_worker(gpu, ngpus_per_node, args):
    global best_acc1
    args.gpu = gpu

    if args.gpu is not None:
        print("Use GPU: {} for training".format(args.gpu))

    if args.distributed:
        if args.dist_url == "env://" and args.rank == -1:
            args.rank = int(os.environ["RANK"])
        if args.multiprocessing_distributed:
            # For multiprocessing distributed training, rank needs to be the
            # global rank among all the processes
            args.rank = args.rank * ngpus_per_node + gpu
        dist.init_process_group(backend=args.dist_backend, init_method=args.dist_url,
                                world_size=args.world_size, rank=args.rank)

    if args.gen_map:
        args.qw = -1
        args.qa = -1
    # create model
    if args.pretrained:
        print("=> using pre-trained model '{}'".format(args.arch))
    else:
        print("=> creating model '{}'".format(args.arch))
    try:
        model = models.__dict__[args.arch](pretrained=args.pretrained)
        args.qw = -1
        args.qa = -1
    except KeyError:
        if 'q' in args.arch and 'seq' not in args.arch:
            model = imagenet_extra_models.__dict__[args.arch](pretrained=args.pretrained, nbits_a=args.qa,
                                                              nbits_w=args.qw,
                                                              q_mode=str_q_mode_map[args.q_mode],
                                                              )
        else:
            model = imagenet_extra_models.__dict__[args.arch](pretrained=args.pretrained)
    print('model:\n=========\n{}\n=========='.format(model))

    if args.gen_map:
        if args.gen_map:
            main_gen_key_map(args, model, [models, imagenet_extra_models])
            return

    # parallel and multi-gpu

    model = distributed_model(model, ngpus_per_node, args)
    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda(args.gpu)
    params = add_weight_decay(model, weight_decay=args.weight_decay, skip_keys=['expand_', 'running_scale', 'alpha'])
    optimizer = torch.optim.SGD(params, args.lr,
                                momentum=args.momentum)

    process_model(model, optimizer, args)

    cudnn.benchmark = True

    df = DataloaderFactory(args)
    train_loader, val_loader, train_sampler = df.product_train_val_loader(df.imagenet2012)
    writer = get_summary_writer(args, ngpus_per_node)
    if (args.qw <= 0 and args.qa <= 0) or args.evaluate:
        if writer is not None:
            get_model_info(model, args, val_loader)
    args.batch_num = len(train_loader)

    scheduler = get_lr_scheduler(optimizer, args)
    dnq_scheduler = get_dnq_scheduler(model, args)

    if args.evaluate:
        validate(val_loader, model, criterion, args)
        return

    for epoch in range(0, args.start_epoch):
        scheduler.step()
        pass
    for epoch in range(args.start_epoch, args.epochs):
        if args.distributed:
            train_sampler.set_epoch(epoch)
        # adjust_learning_rate(optimizer, epoch, args)
        # train for one epoch
        train(train_loader, model, criterion, optimizer, epoch, args, writer)
        scheduler.step()
        dnq_scheduler.step()
        # evaluate on validation set
        acc1, acc5 = validate(val_loader, model, criterion, args)
        if writer is not None:
            writer.add_scalar('val/acc1', acc1, epoch)
            writer.add_scalar('val/acc5', acc5, epoch)
            if args.debug:
                for module_name, module in model.named_modules():
                    if isinstance(module, my_nn.Conv2dDNQ):
                        writer.add_scalar('val/nbits', module.nbits, epoch)
                        break
        # remember best acc@1 and save checkpoint
        is_best = acc1 > best_acc1
        best_acc1 = max(acc1, best_acc1)

        if writer is not None:
            save_checkpoint({
                'epoch': epoch + 1,
                'arch': args.arch,
                'state_dict': model.state_dict(),
                'best_acc1': best_acc1,
                'optimizer': optimizer.state_dict(),
            }, is_best, prefix='{}/{}_w{}a{}'.format(args.log_name, args.arch, args.qw, args.qa))
    if writer is not None:
        writer.close()


if __name__ == '__main__':
    main()
