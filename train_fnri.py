from __future__ import division, print_function

import argparse
import os

import numpy as np
import torch.optim as optim
from atalaya import Logger
from torch.optim import lr_scheduler
from utils import *

from modules import *


def parse_args(args):
    parser = argparse.ArgumentParser()
    # Device arguments
    parser.add_argument('--seed', type=int, default=0,
                        help='Random seed (0 is no random-seed).')
    parser.add_argument('--no-cuda', action='store_true', default=False,
                        help='Disables CUDA training.')

    # Data arguments
    parser.add_argument('--data-folder', type=str, default='',
                        help='Path to the data folder.')
    parser.add_argument('--suffix', type=str, default='armless',
                        help='Suffix for training data (e.g. "armless".')
    parser.add_argument('--num-atoms', type=int, default=15,
                        help='Number of atoms in simulation.')
    parser.add_argument('--dims', type=int, default=6,
                        help=('The number of input dimensions '
                              '(position + velocity).'))
    parser.add_argument('--timesteps', type=int, default=100,
                        help='The number of time steps per sample.')
    parser.add_argument('--dims-clinical', type=int, default=84,
                        help='Number of clinical features.')
    # Training arguments
    parser.add_argument('--epochs', type=int, default=1,
                        help='Number of epochs to train.')
    parser.add_argument('--batch-size', type=int, default=2,
                        help='Number of samples per batch.')
    parser.add_argument('--lr', type=float, default=0.0001,
                        help='Initial learning rate.')
    parser.add_argument('--lr-decay', type=int, default=50,
                        help=('After how many epochs to decay LR by a factor'
                              'of gamma.'))
    parser.add_argument('--gamma', type=float, default=0.5,
                        help='LR decay factor.')
    parser.add_argument('--patience', type=int, default=50,
                        help='Early stopping patience.')
    parser.add_argument('--encoder-dropout', type=float, default=0.0,
                        help='Dropout rate (1 - keep probability).')
    parser.add_argument('--decoder-dropout', type=float, default=0.0,
                        help='Dropout rate (1 - keep probability).')
    parser.add_argument('--temp', type=float, default=0.5,
                        help='Temperature for Gumbel softmax.')
    parser.add_argument('--temp-decay', type=int, default=200,
                        help=('After how many epochs to decay temperature by a'
                              'factor of tau.'))
    parser.add_argument('--tau', type=float, default=0.5,
                        help='Temperature decay factor.')

    # Model arguments
    parser.add_argument('--encoder-hidden', type=int, default=256,
                        help='Number of hidden units.')
    parser.add_argument('--decoder-hidden', type=int, default=256,
                        help='Number of hidden units.')
    parser.add_argument('--skip-first', action='store_true', default=False,
                        help=('Skip first edge type in decoder, '
                              'i.e. it represents no-edge.'))
    parser.add_argument('--hard', action='store_true', default=False,
                        help='Uses discrete samples in training forward pass.')
    parser.add_argument('--prior', action='store_true', default=False,
                        help='Whether to use sparsity prior.')
    parser.add_argument('--edge-types', type=int, default=4,
                        help='The number of edge types to infer.')
    parser.add_argument('--no-factor', action='store_true', default=False,
                        help='Disables factor graph model.')

    # Specific fNRI arguments
    parser.add_argument('--edge-types-list', nargs='+', default=[2, 2, 2],
                        help='The number of edge types to infer.')  # takes arguments from cmd line as: --edge-types-list 2 2
    parser.add_argument('--split-point', type=int, default=0,
                        help='The point at which factor graphs are split up in the encoder')

    # Conditional arguments
    parser.add_argument('--cond-hidden', action='store_true', default=False,
                        help='Conditionates the model in hidden layer.')
    parser.add_argument('--cond-msgs', action='store_true', default=False,
                        help='Conditionates the model on messages level.')

    # Loss arguments
    parser.add_argument('--var', type=float, default=5e-5,
                        help='Output variance.')
    parser.add_argument('--beta', type=float, default=1.0,
                        help='KL-divergence beta factor')
    parser.add_argument('--mse-loss', action='store_true', default=False,
                        help='Use the MSE as the loss')

    # Logger and Grapher arguments (using atalaya)
    # Logger
    parser.add_argument('--logger-folder', type=str, default='',
                        help=('Where to save the trained model, leave empty to'
                              ' not save anything.'))
    parser.add_argument('--no-verbose', action='store_true', default=False,
                        help='Display information in terminal')
    parser.add_argument('--logger-name', type=str, default='exp',
                        help='First part of the logger name (e.g. "exp1".')
    parser.add_argument('--load-params', type=str, default='',
                        help='Where to load the params. ')
    parser.add_argument('--load-folder', type=str, default='',
                        help='Where to load the model. ')
    # Grapher
    parser.add_argument('--grapher', type=str, default='',
                        help='Name of the grapher. Leave empty for no grapher')
    # if visdom
    parser.add_argument('--visdom-url', type=str, default="http://localhost",
                        help='visdom URL (default: http://localhost).')
    parser.add_argument('--visdom-port', type=int, default="8097",
                        help='visdom port (default: 8097)')
    parser.add_argument('--visdom-username', type=str, default='',
                        help='Username of visdom server.')
    parser.add_argument('--visdom-password', type=str, default='',
                        help='Password of visdom server.')

    return parser.parse_args(args)


def run(mode, data_loader, encoder, decoder, optimizer, rel_rec,
        rel_send, log_prior, args):
    history = {key: []
               for key in ['mse', 'nll', 'kl', 'loss', 'KLb_train', 'KLb_blocks']}

    if mode == 'post':
        aggr_posterior = torch.zeros((args.num_atoms**2)-args.num_atoms,
                                     args.edge_types).to(args.device)
        n_samples = 0

    for batch_idx, (data, clinical) in enumerate(data_loader):
        data = data.to(args.device)
        if args.conditional:
            clinical = clinical.to(args.device)

        if mode == 'train':
            optimizer.zero_grad()

        logits = encoder(data, rel_rec, rel_send)

        logits_split = torch.split(logits, args.edge_types_list, dim=-1)
        edges_split = tuple([gumbel_softmax(logits_i, tau=args.temp, hard=args.hard)
                             for logits_i in logits_split])

        edges = torch.cat(edges_split, dim=-1)

        prob_split = [my_softmax(logits_i, -1) for logits_i in logits_split]

        if args.prior:
            loss_kl_split = [kl_categorical(prob_split[type_idx], log_prior[type_idx], args.num_atoms)
                             for type_idx in range(len(args.edge_types_list))]
            loss_kl = sum(loss_kl_split)
        else:
            loss_kl_split = [kl_categorical_uniform(prob_split[type_idx], args.num_atoms,
                                                    args.edge_types_list[type_idx])
                             for type_idx in range(len(args.edge_types_list))]
            loss_kl = sum(loss_kl_split)

            loss_kl_var_split = [kl_categorical_uniform_var(prob_split[type_idx], args.num_atoms,
                                                            args.edge_types_list[type_idx])
                                 for type_idx in range(len(args.edge_types_list))]

        KLb_blocks = KL_between_blocks(prob_split, args.num_atoms)
        history['KLb_train'].append(sum(KLb_blocks).data.item())
        history['KLb_blocks'].append([KL.data.item() for KL in KLb_blocks])

        output = decoder(data, edges, rel_rec, rel_send, clinical)

        loss_nll = nll_gaussian(output, data, args.var)
        loss_nll_var = nll_gaussian_var(output, data, args.var)

        # args.beta = int((loss_nll/loss_kl) / 10)
        if not np.isclose(args.beta, 0, rtol=1e-6):
            loss_kl = args.beta*loss_kl

        loss_mse = F.mse_loss(output, data)

        # if mse_loss == true use it else use elbo
        loss = loss_mse if args.mse_loss else loss_nll + loss_kl

        if mode == 'train':
            loss.backward()
            nn.utils.clip_grad_norm_(
                list(encoder.parameters()) + list(decoder.parameters()), 1.)
            optimizer.step()
        if mode == 'post':
            aggr_posterior = aggr_posterior + edges.sum(0)
            n_samples = n_samples + data.size(0)

        history['loss'].append(loss.item())
        history['mse'].append(loss_mse.item())
        history['nll'].append(loss_nll.item())
        history['kl'].append(loss_kl.item())

    if mode == 'post':
        return None, aggr_posterior/n_samples

    return history, None


def train(epoch, data_loader, encoder, decoder, optimizer, scheduler, rel_rec,
          rel_send, log_prior, args, logger):
    encoder.train()
    decoder.train()

    history, _ = run('train', data_loader, encoder, decoder, optimizer,
                     rel_rec, rel_send, log_prior, args)
    scheduler.step()

    history = logger.register_plots(history, epoch, prefix='train')
    return history['loss']


def test(mode, epoch, data_loader, encoder, decoder, rel_rec, rel_send,
         log_prior, args, logger):
    encoder.eval()
    decoder.eval()

    with torch.no_grad():
        history, aggr_posterior = run(mode, data_loader, encoder, decoder,
                                      None, rel_rec, rel_send,
                                      log_prior, args)

    if mode == 'post':
        return None, aggr_posterior

    history = logger.register_plots(history, epoch, prefix=mode)
    return history['loss'], None


def generations(data_loader, decoder, rel_rec, rel_send, aggr_posterior,
                first_frame_params, args, logger):
    decoder.eval()
    aggr_posterior = aggr_posterior.cpu().detach().numpy()

    path = os.path.join('Results', logger.name)
    os.makedirs(path, exist_ok=True)
    outputs = []
    for batch_idx, (data, clinical) in enumerate(data_loader):
        params = data[:, :, 0, :].to(args.device)
        data = data.to(args.device)
        clinical = clinical.to(args.device)
        edges = np.zeros((data.shape[0], rel_rec.shape[0], args.edge_types))
        for bz in range(data.shape[0]):
            for i in range(rel_rec.shape[0]):
                for et in range(len(args.edge_types_list)):
                    n = args.edge_types_list[et]
                    idx = np.random.choice(n, 1,
                                           p=aggr_posterior[i, et*n:(et+1)*n])
                    edges[bz, i, (et*n)+idx] = 1

        edges = Variable(torch.Tensor(edges)).to(args.device)
        with torch.no_grad():
            output = decoder(data, edges, rel_rec, rel_send, clinical)
        del edges
        mse = F.mse_loss(output, data).item()
        logger.add_scalar('generation_mse', mse, batch_idx+1)

        output = output.data.cpu().detach().numpy()
        outputs.append(output)

    logger.info("Saving data !")

    # np.save(os.path.join(path, 'history.npy'), np.concatenate(history, axis=0))
    np.save(os.path.join(path, 'output.npy'), np.concatenate(outputs, axis=0))


def main(args):
    # get args
    args = parse_args(args)

    logger = Logger(name="{}_{}".format(args.logger_name, args.suffix),
                    path=args.logger_folder,
                    verbose=(not args.no_verbose),
                    grapher=args.grapher,
                    server=args.visdom_url,
                    port=args.visdom_port,
                    username=args.visdom_username,
                    password=args.visdom_password)

    # add parameters to the logger
    logger.add_parameters(args)
    if args.load_folder or args.load_params:
        path_data = args.data_folder
        args = logger.restore_parameters(args.load_params if args.load_params
                                         else args.load_folder)
        args.data_folder = path_data

    # if GPU available -> use device == cuda
    cuda = not args.no_cuda and torch.cuda.is_available()
    args.device = torch.device("cuda" if cuda else "cpu")

    # if random seed use it, args.seed == 0 is non random seed
    if args.seed:
        np.random.seed(args.seed)
        torch.manual_seed(args.seed)
        if cuda:
            torch.cuda.manual_seed(args.seed)

    args.factor = not args.no_factor

    train_loader, val_loader, test_loader = load_data(args.batch_size,
                                                      args.data_folder,
                                                      args.suffix)

    # get mean and std of the first frame on training samples
    first_frame_params = get_params_first_frame(train_loader)

    # Generate off-diagonal interaction graph
    off_diag = np.ones([args.num_atoms, args.num_atoms]) \
        - np.eye(args.num_atoms)

    rel_rec = np.array(encode_onehot(np.where(off_diag)[1]), dtype=np.float32)
    rel_send = np.array(encode_onehot(np.where(off_diag)[0]), dtype=np.float32)
    rel_rec = torch.FloatTensor(rel_rec)
    rel_send = torch.FloatTensor(rel_send)

    args.edge_types_list = list(map(int, args.edge_types_list))
    args.edge_types_list.sort(reverse=True)
    if all((isinstance(k, int) and k >= 1) for k in args.edge_types_list):
        args.edge_types = sum(args.edge_types_list)
    else:
        raise ValueError('Could not compute the edge-types-list')

    # initialize encoder
    encoder = MLPEncoder_multi(args.timesteps * args.dims,
                               args.encoder_hidden,
                               args.edge_types_list,
                               args.encoder_dropout,
                               split_point=args.split_point)

    # add encoder to logger, will save chekpoints and best
    logger.add('encoder', encoder)

    # initialize decoder
    decoder = RNNDecoder_multi(n_in_node=args.dims,
                               n_atoms=args.num_atoms,
                               n_clinical=args.dims_clinical,
                               edge_types=args.edge_types,
                               edge_types_list=args.edge_types_list,
                               n_hid=args.decoder_hidden,
                               cond_hidden=args.cond_hidden,
                               cond_msgs=args.cond_msgs,
                               do_prob=args.decoder_dropout,
                               skip_first=args.skip_first)
    # add decoder to logger, will save chekpoints and best
    logger.add('decoder', decoder)

    # optimizer
    optimizer = optim.Adam(list(encoder.parameters())
                           + list(decoder.parameters()),
                           lr=args.lr)
    logger.add('optimizer', optimizer)

    # scheduler for adam
    scheduler = lr_scheduler.StepLR(optimizer,
                                    step_size=args.lr_decay,
                                    gamma=args.gamma)
    logger.add('scheduler_opti', scheduler)

    # TODO: sheduler for gumbel softmax
    # # scheduler for temperature in gumble softmax
    # scheduler = temp_scheduler.StepLR(optimizer,
    #                                 step_size=args.temp_decay,
    #                                 gamma=args.tau)
    # logger.add('scheduler_tau', scheduler)

    if args.load_folder:
        logger.restore(args.load_folder)

    log_prior = None
    if args.prior:
        prior_et = np.zeros(args.edge_types_list[0])
        prior_et[0] = 0.9
        prior_et[1:] = 0.1 / (args.edge_types_list[0]-1)

        # TODO: hard coded for now
        prior = [prior_et] * len(args.edge_types_list)
        if not all(prior[i].size == args.edge_types_list[i] for i in range(len(args.edge_types_list))):
            raise ValueError('Prior is incompatable with the edge types list')
        logger.info("Using prior: "+str(prior))
        log_prior = []
        for i in range(len(args.edge_types_list)):
            prior_i = prior[i]
            log_prior_i = torch.FloatTensor(np.log(prior_i))
            log_prior_i = torch.unsqueeze(log_prior_i, 0)
            log_prior_i = torch.unsqueeze(log_prior_i, 0)
            log_prior_i = Variable(log_prior_i).to(args.device)
            log_prior.append(log_prior_i)

    encoder.to(args.device)
    decoder.to(args.device)
    rel_rec = Variable(rel_rec).to(args.device)
    rel_send = Variable(rel_send).to(args.device)

    # check if the nri will be cond
    args.conditional = (args.cond_hidden or args.cond_msgs)

    # Train model
    stop_early = 0
    for epoch in range(1, args.epochs+1):
        _ = train(epoch, train_loader, encoder, decoder, optimizer, scheduler,
                  rel_rec, rel_send, log_prior, args, logger)

        val_loss, _ = test('val', epoch, val_loader, encoder, decoder, rel_rec,
                           rel_send, log_prior, args, logger)

        _, _ = test('test', epoch, test_loader, encoder, decoder, rel_rec,
                    rel_send, log_prior, args, logger)

        # store a checkpoint and save if val_loss < min(all previous val_loss)
        best_val_loss = logger.store(val_loss)

        # updating stop_early if not improving in validation set
        stop_early = 0 if best_val_loss else stop_early+1

        if stop_early > args.patience:
            logger.info(("Stopped training because it hasn't improve "
                         "performance in validation set for "
                         "{} epochs").format(args.patience))
            break

    logger.info("Optimization Finished!")

    # test and generation with best model
    # restore best model
    if len(args.load_folder) == 0:
        logger.restore(best=True)
    # load the best model for a specific folder
    else:
        logger.restore(folder=args.load_folder, best=True)

    # obtaining the aggregated posterior
    _, aggr_posterior = test('post', None, train_loader, encoder, decoder,
                             rel_rec, rel_send, log_prior, args, logger)

    # log agg posterior
    aggr_post_2_log = aggr_posterior.cpu().detach().numpy()
    aggr_post_2_log = aggr_post_2_log.reshape(
        aggr_post_2_log.shape[0]*aggr_post_2_log.shape[1]).tolist()
    aggr_post_2_log = [str(val) for val in aggr_post_2_log]
    with open("{}/aggr_posterior.csv".format(logger.logs_dir), "a+") as csv_file:
        csv_file.write("{}\n".format(", ".join(aggr_post_2_log)))

    # generate and computing mse
    generations(test_loader, decoder, rel_rec, rel_send, aggr_posterior,
                first_frame_params, args, logger)

    # close logger
    logger.close()


if __name__ == '__main__':
    import sys
    main(sys.argv[1:])
