import dataclasses

from module.dmv import DMV, DMVOptions
from module.neural_m import NeuralM, NeuralMOptions
from utils.data import ConllDataset, Vocab, BLLIP_POS
from utils.functions import *
from utils.runner import Runner, Model, Logger, RunnerOptions


# NO UPDATE ROOT


@dataclasses.dataclass
class LNMDVModelOptions(RunnerOptions, DMVOptions, NeuralMOptions):
    save_parse_result: bool = False
    use_pair: bool = True
    vocab_path: str = 'data/bllip_vec/vocab+pos.txt'
    pos_path: str = 'data/bllip_vec/pos.txt'
    emb_path: str = 'data/bllip_vec/sub_wordvectors.npy'

    # dim=dim_word_emb. to build `pre_out_child` matrix when use_emb_as_w=True
    # NOT for converting pos array to vectors
    out_pos_emb_path: str = 'data/bllip_vec/posvectors.npy'  # for out, dim=100
    pos_emb_path: str = 'data/bllip_vec/posvec.npy'  # real emb, dim=20

    dmv_batch_size: int = 10240
    reset_neural: bool = False
    neural_stop_criteria: float = 1e-4
    neural_max_subepoch: int = 100
    neural_init_epoch: int = 1

    # pretrained_ds = 'data/wsj10_tr_pred'
    pretrained_ds: str = ''  # 'data/bllip_conll/bllip10clean_full_pretr'

    # overwrite default opotions
    train_ds: str = 'data/bllip_conll/bllip10clean_50k.conll;data/wsj10_tr'
    dev_ds: str = 'data/wsj10_d'
    test_ds: str = 'data/wsj10_te'
    num_lex: int = 390  # not include <UNK> <PAD>

    dim_pos_emb: int = 20
    dim_word_emb: int = 100
    dim_valence_emb: int = 20
    dim_hidden: int = 128
    dim_pre_out_decision: int = 32
    dim_pre_out_child: int = 120
    dropout: float = 0.3
    lr: float = 0.001
    optimizer: str = 'adam'  # overwrited in LNDMVModel.build
    use_pos_emb: bool = True
    use_word_emb: bool = True
    use_valence_emb: bool = True
    use_emb_as_w: bool = True
    freeze_word_emb: bool = True
    freeze_pos_emb: bool = False

    batch_size: int = 1024
    max_epoch: int = 100
    early_stop: int = 10
    compare_field: str = 'likelihood'
    save_best: bool = True

    show_log: bool = True
    show_best_hit: bool = True

    run_dev: bool = True
    run_test: bool = True

    e_step_mode: str = 'viterbi'
    cv: int = 2
    count_smoothing: float = 0.1
    param_smoothing: float = 0.1

class LNDMVModel(Model):
    def __init__(self, o: LNMDVModelOptions, r: Runner):
        self.o = o
        self.r = r

        # store train_data param when eval
        self.model_dec_params = None
        self.model_trans_params = None

    def build(self, nn_only=False):
        word_idx = cp.arange(2, len(self.r.train_ds.word_vocab))[:self.o.num_lex]
        if not nn_only:
            self.dmv = DMV(self.o)
            self.dmv.init_specific(self.r.train_ds.get_len())
            if self.o.use_pair:
                self.converter = get_tag_pair_id_converter(self.r.pos_dict, len(self.r.train_ds.pos_vocab))
            else:
                self.converter = get_tag_id_converter(word_idx, len(self.r.train_ds.pos_vocab))

        self.neural_m = NeuralM(self.o, self.r.word_emb, self.r.out_pos_emb, self.r.pos_emb).cuda()
        # self.neural_m.optimizer = torch.optim.Adam(self.neural_m.parameters(), lr=self.o.lr, betas=(0.5, 0.75))
        # self.neural_m.optimizer = torch.optim.SGD(self.neural_m.parameters(), lr=self.o.lr, momentum=0.9)

        if self.o.use_pair:
            word_idx, pos_idx = [], []
            for word_id, pos_ids in self.r.pos_dict.items():
                word_idx.extend([word_id] * len(pos_ids))
                pos_idx.extend(pos_ids)
            word_idx = torch.tensor(word_idx, device='cuda')
            pos_idx = torch.tensor(pos_idx, device='cuda')
            self.neural_m.set_lex(word_idx, pos_idx)
        else:
            self.neural_m.set_lex(cp2torch(word_idx), None)

    def train_init(self, epoch_id, dataset):
        dataset.build_batchs(self.o.dmv_batch_size, False, True)
        if self.o.reset_neural:
            self.build(nn_only=True)
        if self.dmv.initializing:
            self.r.best = None
            self.r.best_epoch = -1
        if self.dmv.initializing and epoch_id >= self.o.neural_init_epoch:
            self.dmv.initializing = False
            self.r.logger.write("finishing initialization")
        self.dmv.reset_root_counter()

        # EXPERIMENT
        # self.neural_m.optimizer = torch.optim.SGD(self.neural_m.parameters(), lr=self.o.lr, momentum=0.9)

    def train_one_step(self, epoch_id, batch_id, one_batch):
        batch_size = len(one_batch[0])

        idx = np.arange(batch_size)

        id_array = one_batch[0]
        pos_array = cpasarray(one_batch[1])
        pos_array_torch = cp2torch(pos_array)
        word_array = cpasarray(one_batch[2])
        word_array_torch = cp2torch(word_array)
        len_array = one_batch[3]
        len_array_torch = torch.tensor(len_array, dtype=torch.long, device='cuda')
        tag_array = self.converter(word_array, pos_array)
        tag_array_torch = cp2torch(tag_array)
        max_len = np.max(len_array)

        if self.dmv.initializing:
            ll = self.dmv.e_step(id_array, pos_array, len_array)
        else:
            self.neural_m.eval()
            trans_params, dec_params = [], []
            with torch.no_grad():
                for i in range(0, batch_size, self.o.batch_size):
                    sub_idx = slice(i, i + self.o.batch_size)
                    arrays = {'pos': pos_array_torch[sub_idx],
                              'word': word_array_torch[sub_idx],
                              'len': len_array_torch[sub_idx]}

                    dec_param, trans_param = self.neural_m(arrays, tag_array_torch[sub_idx])
                    trans_params.append(trans_param)
                    dec_params.append(dec_param)
            trans_param = cpfempty((batch_size, max_len + 1, max_len + 1, self.o.cv))
            dec_param = cpfempty((batch_size, max_len + 1, 2, 2, 2))
            offset = 0
            for t, d in zip(trans_params, dec_params):
                _, batch_len, *_ = t.shape
                t = torch2cp(t)
                d = torch2cp(d)
                trans_param[offset: offset + self.o.batch_size, 1:batch_len + 1, 1:batch_len + 1] = t
                dec_param[offset: offset + self.o.batch_size, 1:batch_len + 1] = d
                offset += self.o.batch_size
            root_param = cp.expand_dims(self.dmv.root_param, 0)
            root_scores = cp.expand_dims(cp.take_along_axis(root_param, self.dmv.input_gaurd(pos_array), 1), -1)
            trans_param[:, 0, :, :] = root_scores
            trans_param[:, :, 0, :] = -cp.inf

            ll = self.dmv.e_step_using_unmnanaged_score(tag_array, len_array, trans_param, dec_param)

        self.dmv.batch_dec_trace = cp.sum(self.dmv.batch_dec_trace, axis=2)
        dec_trace = cp2torch(self.dmv.batch_dec_trace)
        trans_trace = cp2torch(self.dmv.batch_trans_trace)

        self.neural_m.train()
        loss_previous = 0.
        for sub_run in range(self.o.neural_max_subepoch):
            loss_current = 0.

            np.random.shuffle(idx)
            for i in range(0, batch_size, self.o.batch_size):
                self.neural_m.optimizer.zero_grad()
                # sub_idx = slice(i, i + self.o.batch_size)
                sub_idx = idx[i: i + self.o.batch_size]
                arrays = {'pos': pos_array_torch[sub_idx],
                          'word': word_array_torch[sub_idx],
                          'len': len_array_torch[sub_idx]}
                traces = {'decision': dec_trace[sub_idx], 'transition': trans_trace[sub_idx]}

                loss = self.neural_m(arrays, tag_array_torch[sub_idx], traces=traces)
                loss_current += loss.item()
                loss.backward()
                self.neural_m.optimizer.step()

            if loss_previous > 0.:
                diff_rate = abs(loss_previous - loss_current) / loss_previous
                if diff_rate < self.o.neural_stop_criteria and not self.dmv.initializing:
                    break
            loss_previous = loss_current

        return {'loss': loss_current, 'likelihood': ll, 'runs': sub_run + 1}

    def train_callback(self, epoch_id, dataset, result):
        self.dmv.m_step()
        return {'loss': sum(result['loss']) / len(result['loss']),
                'likelihood': sum(result['likelihood']),
                'runs': sum(result['runs']) / len(result['runs'])}

    def eval_init(self, mode, dataset):
        if mode == 'test' and self.r.best is not None and self.o.save_best:
            self.load(self.r.best_path)

        # backup train status
        self.model_dec_params = self.dmv.all_dec_param
        self.model_trans_params = self.dmv.all_trans_param

        # init eval status
        self.neural_m.eval()
        self.dmv.init_specific(dataset.get_len())

    def eval_one_step(self, mode, batch_id, one_batch):
        with torch.no_grad():
            pos_array = cpasarray(one_batch[1])
            word_array = cpasarray(one_batch[2])
            tag_array = self.converter(word_array, pos_array)

            pos_array = cp2torch(pos_array)
            word_array = cp2torch(word_array)

            len_array = torch.tensor(one_batch[3], device='cuda')

            arrays = {'pos': pos_array, 'word': word_array, 'len': len_array}
            dec_param, trans_param = self.neural_m(arrays, cp2torch(tag_array))

            id_array = one_batch[0]
            len_array = one_batch[3]
            dec_param = dec_param.cpu().numpy()
            trans_param = trans_param.cpu().numpy()
            self.dmv.put_decision_param(id_array, dec_param, len_array)
            self.dmv.put_transition_param(id_array, trans_param, len_array)

            out = self.dmv.parse(id_array, tag_array, len_array)
            out['likelihood'] = self.dmv.e_step(id_array, tag_array, len_array)
        return out

    def eval_callback(self, mode, dataset, result):
        ll = sum(result['likelihood'])
        del result['likelihood']
        # calculate uas
        for k in result:
            result[k] = result[k][0]
        acc, _, _ = calculate_uas(result, dataset)
        if self.o.save_parse_result and mode == 'test':
            print_to_file(result, self.r.dev_ds, os.path.join(self.r.workspace, 'parsed.txt'))

        # restore train status
        self.dmv.all_dec_param = self.model_dec_params
        self.dmv.all_trans_param = self.model_trans_params
        return {'uas': acc * 100, 'likelihood': ll}

    def init_param(self, dataset):
        word_idx = cp.arange(2, len(dataset.word_vocab))
        if self.o.use_pair:
            converter = get_init_param_converter_v2(get_tag_pair_id_converter, self.r.pos_dict, len(dataset.pos_vocab))
        else:
            converter = get_init_param_converter_v2(get_tag_id_converter, word_idx, len(dataset.pos_vocab))
        if self.r.pretrained_ds:
            self.dmv.init_pretrained(self.r.pretrained_ds, converter)
        else:
            dataset.build_batchs(self.o.batch_size, same_len=True)
            self.dmv.init_param(dataset, converter)

    def save(self, folder_path):
        self.dmv.save(folder_path)
        self.neural_m.save(folder_path)

    def load(self, folder_path):
        self.dmv.load(folder_path)
        self.neural_m.load(folder_path)

    def __str__(self):
        return f'LNDMV_{self.o.e_step_mode}_{self.o.cv}_{len(self.r.train_ds.word_vocab) - 2}'


class LNDMVModelRunner(Runner):
    def __init__(self, o: LNMDVModelOptions):
        if o.use_softmax_em:
            from utils import common
            common.cpf = cp.float64

        m = LNDMVModel(o, self)
        super().__init__(m, o, Logger(o))

    def load(self):
        if self.o.use_pair:
            wordpos_vocab_list = [w.strip().split() for w in open(self.o.vocab_path)][:self.o.num_lex + 2]
            word_vocab_list = [wp[0] for wp in wordpos_vocab_list]
            word_vocab = Vocab.from_list(word_vocab_list, unk='<UNK>', pad='<PAD>')
            self.pos_dict = {word_vocab[wp[0]]: list(
                map(BLLIP_POS.__getitem__, wp[1:])) for wp in wordpos_vocab_list if len(wp) > 1}
        else:
            word_vocab_list = [w.strip() for w in open(self.o.vocab_path)][:self.o.num_lex + 2]
            word_vocab = Vocab.from_list(word_vocab_list, unk='<UNK>', pad='<PAD>')

        self.train_ds = ConllDataset(self.o.train_ds, pos_vocab=BLLIP_POS, word_vocab=word_vocab)

        self.dev_ds = ConllDataset(self.o.dev_ds, pos_vocab=BLLIP_POS, word_vocab=word_vocab)
        self.test_ds = ConllDataset(self.o.test_ds, pos_vocab=BLLIP_POS, word_vocab=word_vocab)

        if self.o.pretrained_ds:
            self.pretrained_ds = ConllDataset(self.o.pretrained_ds, pos_vocab=BLLIP_POS, word_vocab=word_vocab)
        else:
            self.pretrained_ds = None

        self.dev_ds.build_batchs(self.o.batch_size)
        self.test_ds.build_batchs(self.o.batch_size)

        if self.o.use_pair:
            self.o.num_lex = sum([len(p) for p in self.pos_dict.values()])
        self.o.max_len = 10
        self.o.num_tag = len(BLLIP_POS) + self.o.num_lex

        if self.o.emb_path:
            self.word_emb = np.load(self.o.emb_path)[:self.o.num_lex + 2]
        else:
            self.word_emb = None
        self.out_pos_emb = np.load(self.o.out_pos_emb_path) if self.o.out_pos_emb_path else None
        self.pos_emb = np.load(self.o.pos_emb_path) if self.o.pos_emb_path else None


if __name__ == '__main__':
    use_torch_in_cupy_malloc()

    options = LNMDVModelOptions()
    options.parse()

    runner = LNDMVModelRunner(options)
    if options.pretrained_ds:
        runner.logger.write('init with acc:')
        runner.evaluate('dev')
        runner.evaluate('test')
    runner.start()
