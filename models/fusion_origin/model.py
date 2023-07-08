from utils.interface import EMOCAModel
from utils.config_loader import GBL_CONF, PATH
from transformers import Wav2Vec2Model
from os.path import join
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import math
from .attention import TransformerBlock



def apply_V_mask(lstm_out: torch.Tensor):
    mask = torch.linspace(0, lstm_out.size(1), steps=lstm_out.size(1), device=lstm_out.device, requires_grad=False) / lstm_out.size(1)
    (b,_,_,p) = lstm_out.size()
    mask = mask.view(1,-1,1).expand(b,-1,p) # batch, seqs_len, 1, params
    return lstm_out[:,:,0,:] * mask + lstm_out[:,:,1,:] * (1.0 - mask)


def zero_padding(t:torch.tensor, pad_size, dim=-1, t_first=True):
    assert(t.size(dim) <= pad_size)
    dev = t.device
    if t.size(dim) == pad_size:
        return t
    if t.dim() == 1:
        if t_first:
            return torch.cat([t, torch.zeros((pad_size-t.size(0)), device=dev)])
        else:
            return torch.cat([torch.zeros((pad_size-t.size(0)), device=dev),t])
    else:
        p_size = list(t.size())
        p_size[dim] = pad_size - t.size(dim)
        p_size = torch.Size(p_size)
        if t_first:
            return torch.cat([t, torch.zeros(p_size, device=dev)], dim=dim)
        else:
            return torch.cat([torch.zeros(p_size, device=dev), t], dim=dim)


class EmoPredLayer(nn.Module):
    def __init__(self, in_channels, out_channels,dp=0.1):
        super(EmoPredLayer, self).__init__()
        self.c_emo = out_channels
        self.lstm = nn.LSTM((in_channels), (128), batch_first=True, bidirectional=True, proj_size=out_channels)
        self.dropout = nn.Dropout(dp)
        # manually norm
        self.emo_min = -2
        self.emo_max = 2

    def forward(self, x, seq_len):
        out,_ = self.lstm(self.dropout(x))
        out = out.view(out.size(0), out.size(1), 2, out.size(2) // 2)
        out = apply_V_mask(out)
        out = (out + 1) * 0.5 * (self.emo_max - self.emo_min) + self.emo_min
        # parse
        out_emo_logits = torch.zeros((seq_len.sum(), self.c_emo), device=x.device)
        for idx in range(seq_len.size(0)):
            start = 0 if idx == 0 else seq_len[:idx].sum()
            out_emo_logits[start:start+seq_len[idx],:] = out[idx, 0:seq_len[idx],:]
        return out_emo_logits # not the last output

class WavLayer(nn.Module):
    def __init__(self, in_channels, out_channels,dp=0.1):
        super(WavLayer, self).__init__()
        self.den1 = nn.Linear(in_channels*3, 256)
        self.relu = nn.ReLU()
        self.norm = nn.LayerNorm(256)
        self.den2 = nn.Linear(256, out_channels)
        self.dropout = nn.Dropout(dp)

    def forward(self, x):
        x = self.norm(self.relu(self.dropout(self.den1(torch.reshape(x, (x.size(0), -1))))))
        return self.den2(x)

class EmoEmbeddingLayer(nn.Module):
    """
    EmoEmbeddingLayer
    input:
        emo_logits: Batch, seq_len, emo_channel
    output:
        Batch, seq_len, out_hidden
    """
    def __init__(self, emo_channels, out_hidden=768, in_hidden=768, dp=0.1):
        super().__init__()
        self.c_emo = emo_channels
        self.hid = in_hidden
        self.style_embedding = nn.Parameter(torch.zeros((1,emo_channels, in_hidden)), requires_grad=True)
        torch.nn.init.xavier_normal_(self.style_embedding)
        self.dropout = nn.Dropout(dp)
        
    
    def forward(self, emo_logits, seqs_len):
        #assert(emo_logits.dim()==3)
        emo_logits = self.dropout(emo_logits)
        try:
            assert(emo_logits.size(0) == torch.sum(seqs_len))
        except Exception as e:
            print('emo_logits:', emo_logits.size(), ' seqs_len:', seqs_len)
            min_s = min(emo_logits.size(0), torch.sum(seqs_len))
            emo_logits = emo_logits[:min_s,:]
            if seqs_len.size(0) == 1:
                seqs_len[0] = min_s
        out_emo_logits = torch.zeros(seqs_len.size(0), seqs_len.max(), self.c_emo, device=emo_logits.device)
        for idx in range(seqs_len.size(0)):
            start = 0 if idx == 0 else seqs_len[:idx].sum()
            out_emo_logits[idx, 0:seqs_len[idx],:] = emo_logits[start:start+seqs_len[idx],:]
        ebd = self.style_embedding.expand(seqs_len.size(0), self.c_emo, self.hid).to(emo_logits.device)
        return torch.bmm(out_emo_logits, ebd)

class StyleExtractor(nn.Module):
    def __init__(self, dim_hidden, dim_output, num_heads, n_blocks, dp):
        super().__init__()
        self.blocks = torch.nn.ModuleList(
            [TransformerBlock(dim_hidden, num_heads, dim_hidden*4, dp, mode='self') for _ in range(n_blocks)])
        self.den = nn.Linear(dim_hidden, dim_output)
        self.mask_ebd = nn.Parameter(torch.zeros((1,1, dim_hidden)), requires_grad=False)
        self.n_blocks = n_blocks
        nn.init.xavier_uniform_(self.mask_ebd)
    
    # x: batch, seq_len, features
    # return: batch, out_features
    def forward(self, x):
        # append mask ebd
        x = torch.cat([self.mask_ebd.expand(x.size(0), -1,-1),x], dim=-2)
        for idx in range(self.n_blocks):
            x = self.blocks[idx].forward(x, None, None)
        return self.den(x[:, 0, :])

class ParamPredictor(nn.Module):
    def __init__(self, dim_style=128, dim_aud=128, dim_out=56):
        super().__init__()
        self.convs = nn.Sequential(
            nn.Conv1d(dim_style+dim_aud, 128, 1),
            nn.ReLU(),
            nn.Conv1d(128, 128, 3, padding=1, groups=8),
            nn.ReLU(),
            nn.Conv1d(128, 56, 1)
        )

    def forward(self, wav_out, style):
        wav_out = wav_out.permute(0,2,1) # batch, feature, seq_len
        wav_out = torch.cat([wav_out, style.unsqueeze(-1).expand(wav_out.size(0), -1, wav_out.size(-1))], dim=-2)
        # feature dim=style + audio

        return self.convs(wav_out).permute(0,2,1) # batch, seq_len, feature

class ParamFixer(nn.Module):
    def __init__(self, dim_emo_ebd=128, dim_params=56):
        super().__init__()
        self.norm = nn.LayerNorm(dim_params+dim_emo_ebd)
        self.convs = nn.Sequential(
            nn.Conv1d(dim_emo_ebd+dim_params, 128, 1),
            nn.ReLU(),
            nn.Conv1d(128, 128, 3, padding=1, groups=8),
            nn.ReLU(),
            nn.Conv1d(128, dim_params, 1)
        )

    def forward(self, params, emo_logits):
        # print('params:', params.size(), 'emo_logits:', emo_logits.size())
        params_in = torch.cat([params, emo_logits], dim=-1)
        params_in = self.norm(params_in).permute(0,2,1)
        return (self.convs(params_in) + params.permute(0,2,1)).permute(0,2,1)

    
class Model(nn.Module):
    def __init__(self, debug=0):
        super(Model, self).__init__()
        self.model_name = 'fusion_origin'
        self.config = GBL_CONF['model'][self.model_name]
        self.out_norm = None
        self.wav_fea_channels = self.config['wav_fea_channels']
        self.params_channels = self.config['params_channels']
        self.hidden = self.config['hidden']
        self.dim_style = self.config['dim_style']
        self.emo_channels = self.config['emo_channels']
        self.dp = self.config['dp']
        self.debug = debug
        self.wav_len_per_seq=16000/30.0
        self._norm_check = False

        try:
            wav2vec_path = PATH['3rd']['wav2vec2']['pretrained']
            model = Wav2Vec2Model.from_pretrained(pretrained_model_name_or_path=None, \
                config=join(wav2vec_path,'config.json'), state_dict=torch.load(join(wav2vec_path,'pytorch_model.bin')))
        except Exception as e:
            print(e)
            print('ignore wav2vec2 warning')
        self.fea_extractor = model.feature_extractor
        for p in self.fea_extractor.parameters(recurse=True): # freeze wav2vec2
            p.requires_grad = False
        self.wav_layer = WavLayer(self.wav_fea_channels, self.hidden, dp=self.dp)
        self.pred_layer = EmoPredLayer(self.hidden, self.emo_channels, dp=self.dp)
        self.style_layer = StyleExtractor(self.hidden, self.dim_style, 1, 4, dp=self.dp)
        self.param_predictor = ParamPredictor(dim_style=self.dim_style, dim_aud=self.hidden, dim_out=self.params_channels)
        self.param_fixer = ParamFixer(dim_emo_ebd=self.hidden, dim_params=56)

        #for p in self.pred_layer.parameters(recurse=True):
        #    p.requires_grad = False
        self.emo_ebd_layer = EmoEmbeddingLayer(self.emo_channels, out_hidden=self.hidden, in_hidden=self.hidden, dp=self.dp)
        # opts
        self.opt_gen = optim.Adam(list(self.wav_layer.parameters(recurse=True))
            + list(self.param_predictor.parameters(recurse=True))
            + list(self.emo_ebd_layer.parameters(recurse=True))
            + list(self.param_fixer.parameters(recurse=True))
            + list(self.pred_layer.parameters(recurse=True))
        )
        self.opt_style = optim.Adam(
            list(self.style_layer.parameters(recurse=True))
        )
        

    def batch_forward(self, in_dict): # used in training phase
        # generate emotion tensor for no-video dataset
        assert('emo_logits_conf' in in_dict.keys())
        return self.forward(in_dict)
    
    def smooth(self, params):
        return torch.nn.functional.conv1d(
            params.permute(0,2,1), torch.ones((56,1,3), device=params.device)/3, padding=1, groups=56).permute(0,2,1)

    def out_norm_apply(self, params):
        return self.out_norm['min'] + (params+1) *0.5 * (self.out_norm['max'] - self.out_norm['min'])

    def test_forward(self, in_dict):
        assert(in_dict['wav'].size(0) == 1) # batch_size should be 1
        assert('emo_logits_conf' in in_dict.keys())
        '''
        emo tensor conf:
        no_use: output without fixer
        use: use input emo-logits, use predicted emo-logits if input is None
        one-hot: use predicted emo-logits with one-hot enhancement
        '''

        if 'code_dict' in in_dict.keys() and in_dict['code_dict'] is not None:
            if not isinstance(in_dict['code_dict'], list):
                in_dict['code_dict'] = [in_dict['code_dict']]
        else:
            in_dict['code_dict'] = None

        if in_dict['emo_logits_conf'] == ['one_hot']:
            if in_dict.get('emo_logits') is None:
                in_dict['pred_only'] = True
                in_dict['emo_logits'] = self.forward(in_dict)
                in_dict['pred_only'] = False
            assert(in_dict.get('emo_label') is not None)
            # TODO
            dan_labels = GBL_CONF['inference']['dan']['emo_label']
            label_dict = {label:idx for idx, label in enumerate(dan_labels)}
            emo_index = label_dict[in_dict['emo_label']]
            print('emo label:', in_dict['emo_label'])
            in_dict['emo_logits'] -= in_dict['emo_logits'].mean(dim=0) # normalize
            if in_dict.get('intensity') is None:
                intensity = GBL_CONF['inference']['inference_mode']['default_intensity'] # 0 to 1
            else:
                intensity = in_dict['intensity']
            in_dict['emo_logits'][:, :] -= intensity
            in_dict['emo_logits'][:, emo_index] += (4*intensity)
            in_dict['emo_logits_conf'] = ['use']
        
        in_dict['smooth'] = False if 'smooth' not in in_dict.keys() or in_dict['smooth'] != True else True
        out =  self.forward(in_dict)
        return out
    
    def forward(self, in_dict):
        wavs = in_dict.get('wav')
        seqs_len = in_dict.get('seqs_len')
        emo_logits = in_dict.get('emo_logits')
        code_dict = in_dict.get('code_dict')
        pred_only = in_dict.get('pred_only')
        smooth = in_dict.get('smooth')
        emo_logits_conf = in_dict.get('emo_logits_conf')

        assert(wavs is not None and seqs_len is not None)
        dev = wavs.device # regard as input device
        output = {}
        # input check
        assert(wavs.dim() == 2)
        if emo_logits is not None:
            emo_logits = emo_logits.to(wavs.device)
        seq_length = torch.max(seqs_len).item()

        # generate mask
        output['mask'] = self.get_output_mask(seqs_len).to(dev)

        clip_len = round(self.wav_len_per_seq)
        if round(seq_length*self.wav_len_per_seq) >= wavs.size(1):
            wavs = zero_padding(wavs, round(seq_length*self.wav_len_per_seq), dim=1)
        wavs = zero_padding(wavs, round(self.wav_len_per_seq)+wavs.size(1), dim=1, t_first=False)

        wavs_input_list = []
        assert((seq_length+1)*clip_len < wavs.size(1))
        for tick in range(seq_length):
            wavs_input_list.append(wavs[:, tick*clip_len:(tick+2)*clip_len].clone())
        wavs_input = torch.stack(wavs_input_list, dim=1)
        
        wav_out = []
        for tick in range(seq_length):
            wav_fea = self.fea_extractor(wavs_input[:,tick,:]).transpose(-1,-2) # batch_size, fea_len, 512
            wav_out.append(self.wav_layer(wav_fea).unsqueeze(1)) # batch, 1, out_fea
        wav_out = torch.cat(wav_out, dim=1) # batch_size, video_seq_len, out_fea
        pred = self.pred_layer(wav_out.detach(), seqs_len)
        if pred_only:
            return pred
        else:
            output['pred_emo_logits'] = pred
        
        if emo_logits_conf == ['use'] and emo_logits is None:
            emo_logits = pred
            in_dict['emo_logits'] = pred
        
        style = self.style_layer(wav_out)
        param_out = self.param_predictor(wav_out, style)

        if emo_logits is not None: # not all sample use emo_logits
            emo_ebd = self.emo_ebd_layer(emo_logits, seqs_len).expand(wav_out.size(0),-1,-1)
            param_emo = self.param_fixer(param_out, emo_ebd)
            for idx in range(len(emo_logits_conf)):
                if emo_logits_conf[idx] == 'no_use': # TODO
                    param_emo[idx,...] = param_out[idx,...]
        else:
            param_emo = None

        # smooth, only enable in test phase
        if smooth:
            param_out = self.smooth(param_out)
            if param_emo is not None:
                param_emo = self.smooth(param_emo)
            

        if self.out_norm is not None:
            if self._norm_check == False or self.out_norm['min'].device != dev:
                self.out_norm['min'] = self.out_norm['min'].to(dev)
                self.out_norm['max'] = self.out_norm['max'].to(dev)
                self._norm_check = True
            param_out = self.out_norm_apply(param_out)
            if param_emo is not None:
                param_emo = self.out_norm_apply(param_emo)

        if param_emo is not None:
            output['params'] = param_emo * output['mask']
        else:
            output['params'] = param_out * output['mask']

        output['params_ori'] = param_out * output['mask']
        
        if code_dict is not None:
            # replace code_dict
            for idx in range(len(code_dict)):
                try: 
                    assert(code_dict[idx]['expcode'].size(0) == seqs_len[idx])
                except AssertionError as e:
                    print('codedict error', code_dict[idx]['expcode'].size(0), seqs_len[idx], seqs_len)
                # code_dict[idx]['expcode'] = lstm_out[idx, :seqs_len[idx], :50]
                code_dict[idx]['expcode'] = output['params'][idx, :seqs_len[idx], :50]
                code_dict[idx]['posecode'][:,3] = output['params'][idx, :seqs_len[idx], 53]
                # code_dict[idx]['posecode'][:,[4,5]] = 0 # fixed dim

            output['code_dict'] = code_dict
            
        return output

    @classmethod
    def from_configs(cls, config):
        return cls(*tuple(config))

    def get_configs(self):
        # update out norm
        self.config[-2] = self.out_norm
        return self.config

    def get_opt_list(self):
        return [self.opt_gen, self.opt_style]

    def get_output_mask(self, seqs_len):
        max_seqs_len = max(seqs_len)
        # mask for seqs_len = tensor([3,2,5])
        # [[T,T,T,F,F],
        #  [T,T,F,F,F],
        # [T,T,T,T,T]]
        mask = torch.arange(0, max_seqs_len, 1).long().unsqueeze(0).expand(seqs_len.size(0), max_seqs_len) < seqs_len.unsqueeze(0).transpose(0,1)
        mask = mask.unsqueeze(dim=-1).expand(mask.size(0), mask.size(1), 56)
        return mask # no device

    def set_norm(self, norm_dict, device):
        self.out_norm = norm_dict
        self.out_norm['min'] = self.out_norm['min'].to(device)
        self.out_norm['max'] = self.out_norm['max'].to(device)

    def set_emoca(self, emoca):
        pass