import os
import random
from torch.utils.dlpack import to_dlpack, from_dlpack
from utils.common import *


def get_tag_id_converter(word_idx, num_pos):
    """
    :param word_idx: the word_idx merged to pos_idx.
      pos_idx ranges from 0 to n-1, then
      word_idx will be remapped to n, n+1, ...
    :return: cp.ndarray
    """
    assert isinstance(word_idx, cp.ndarray)

    def converter(word_array, pos_array):
        in_mask = cp.in1d(word_array, word_idx).reshape(word_array.shape)
        group_array = cp_mask_merge(word_array - 2 + num_pos, pos_array, in_mask)
        return group_array

    return converter


def get_init_param_converter(word_idx, num_pos):
    _converter = get_tag_id_converter(word_idx, num_pos)

    def converter(arrays):
        word_array = cp.asarray(arrays[2])
        pos_array = cp.asarray(arrays[1])
        return _converter(word_array, pos_array)
    return converter


def make_mask(seq_length, max_len=None):
    if max_len is None:
        max_len = seq_length.max()
    batch_size = seq_length.shape[0]
    seq_range = torch.arange(0, max_len, device=seq_length.device)
    seq_range = seq_range.unsqueeze(0).expand(batch_size, max_len)
    seq_length = seq_length.unsqueeze(1)
    return seq_range < seq_length


def set_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False


# math op
cp_logsumexp = cp.ReductionKernel(
    'T x', 'T y', 'exp(x)', 'a+b', 'y=log(a)', '0', 'logsumexp')

cp_mask_merge = cp.ElementwiseKernel(
    'T iftrue, T iffalse, bool mask', 'T z', 'z = mask?iftrue:iffalse', 'mask_merge')


def safe_logsumexp(a, axis):
    r = cp_logsumexp(a, axis=axis)
    r[cp.isnan(r)] = -cp.inf
    return r


def safe_logaddexp(a, b):
    r = cp.logaddexp(a, b)
    r[cp.isnan(r)] = -cp.inf
    return r

# obj op


def cp2torch(a):
    if a.dtype == cpi:
        a = a.astype(cp.int64)
    return from_dlpack(a.toDlpack())


def torch2cp(a):
    return cp.fromDlpack(to_dlpack(a))


def _torch_alloc(size):
    # from github: chainer/chainer-pytorch-migration
    device = cp.cuda.Device().id  # default cuda device
    tensor = torch.empty(size, dtype=torch.uint8, device=device)
    return cp.cuda.MemoryPointer(cp.cuda.UnownedMemory(tensor.data_ptr(), size, tensor), 0)


def use_torch_in_cupy_malloc():
    cp.cuda.set_allocator(_torch_alloc)


def use_mempool_in_cupy_malloc():
    cp.cuda.set_allocator(cp.get_default_memory_pool().malloc)


def calculate_uas(predicted, gold):
    correct_counter = 0
    total_counter = 0
    for j in gold:
        ps = predicted[j.id][1:j.len + 1]
        gs = j.entries
        for i, e in enumerate(gs):
            if ps[i] == e.parent_id:
                correct_counter += 1
            total_counter += 1
    accuracy = correct_counter / total_counter
    return accuracy, correct_counter, total_counter


def print_to_file(predicted, gold, outfile):
    with open(outfile, 'w') as f:
        for id in range(len(gold.instances)):
            j = gold[id]
            ps = predicted[j.id][1:j.len + 1]
            gs = j.entries
            word_len = []
            for g in gs:
                word_len.append(len(g.norm))
                f.write(f'{g.norm}  ')
            f.write('\n')
            for g, l in zip(gs, word_len):
                f.write(f'{g.parent_id:<{l+2}d}')
            f.write('\n')
            for h, l in zip(ps, word_len):
                f.write(f'{h:<{l+2}d}')
            f.write('\n')


def make_sure_dir_exists(path):
    if not os.path.exists(path):
        os.makedirs(path)
    elif not os.path.isdir(path):
        raise RuntimeError(f"{path} is exists but not a directory")


def kl_between_gaussian(mean1, cov1, mean2, cov2):
    # all args have the same shape
    batch_size, real_len, tag_dim = mean1.shape

    mean_diff = (mean1 - mean2)
    cov2_inv = 1 / cov2

    kl = 0.5 * (torch.log(cov2) - torch.log(cov1) - 1 + cov1 * cov2_inv
                + torch.pow(mean_diff, 2) * cov2_inv)
    return kl
