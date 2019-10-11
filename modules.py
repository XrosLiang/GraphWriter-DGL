import torch
import math
import dgl.function as fn
from dgl.nn.pytorch import edge_softmax
from utlis import *
from torch import nn

class MSA(nn.Module):
    def __init__(self, args, mode='normal'):
        super(MSA, self).__init__()
        if mode=='copy':
            nhead, head_dim = 1, args.nhid
            qninp, kninp = args.dec_ninp, args.nhid
        if mode=='normal':
            nhead, head_dim = args.nhead, args.head_dim
            qninp, kninp = args.nhid, args.nhid
            self.WO = nn.Linear(nhead*head_dim, args.nhid)
        self.WQ = nn.Linear(qninp, nhead*head_dim)
        self.WK = nn.Linear(kninp, nhead*head_dim)
        self.WV = nn.Linear(kninp, nhead*head_dim)
        self.args, self.nhead, self.head_dim, self.mode = args, nhead, head_dim, mode

    def forward(self, inp1, inp2, mask=None):
        B, L2, H = inp2.shape
        NH, HD = self.nhead, self.head_dim
        q, k, v = self.WQ(inp1), self.WK(inp2), self.WV(inp2)
        L1 = 1 if inp1.ndim==2 else inp1.shape[1]
        q = (q / math.sqrt(HD)).view(B, L1, NH, HD).permute(0, 2, 1, 3) 
        k = k.view(B, L2, NH, HD).permute(0, 2, 3, 1)
        v = v.view(B, L2, NH, HD).permute(0, 2, 1, 3)
        pre_attn = torch.matmul(q,k)
        if mask is not None:
            pre_attn = pre_attn.masked_fill(mask[:,None,None,:], -1e8)
            #pre_attn = pre_attn.masked_fill(mask[:,None,None,:], float('-inf'))
        if self.mode=='copy':
            return pre_attn.squeeze(1)
        else:
            alpha = torch.softmax(pre_attn, -1)
            attn = torch.matmul(alpha, v).permute(0, 2, 1, 3).contiguous().view(B,L1,NH*HD)
            ret = self.WO(attn)
            if inp1.ndim==2:
                return ret.squeeze(1)
            else:
                return ret

class BiLSTM(nn.Module):
    def __init__(self, args, enc_type='title'):
        super(BiLSTM, self).__init__()
        self.enc_type = enc_type
        self.drop = nn.Dropout(args.emb_drop)
        self.bilstm = nn.LSTM(args.nhid, args.nhid//2, bidirectional=True, \
                num_layers=args.enc_lstm_layers, batch_first=True)
 
    def forward(self, inp, mask, ent_len=None):
        inp = self.drop(inp)
        lens = (mask==0).sum(-1).long().tolist()
        pad_seq = pack_padded_sequence(inp, lens, batch_first=True, enforce_sorted=False)
        y, (_h, _c) = self.bilstm(pad_seq)
        if self.enc_type=='title':
            y = pad_packed_sequence(y, batch_first=True)[0]
            return y
        if self.enc_type=='entity':
            _h = _h.transpose(0,1).contiguous()
            _h = _h[:,-2:].view(_h.size(0), -1) # two directions of the top-layer
            ret = pad(_h.split(ent_len), out_type='tensor')
            return ret

class GAT(nn.Module):
    def __init__(self,
                 in_feats,
                 out_feats,
                 num_heads,
                 ffn_drop=0.,
                 attn_drop=0.,
                 trans=True):
        super(GAT, self).__init__()
        self._num_heads = num_heads
        self._in_feats = in_feats
        self._out_feats = out_feats
        self.q_proj = nn.Linear(in_feats, num_heads * out_feats, bias=False)
        self.k_proj = nn.Linear(in_feats, num_heads * out_feats, bias=False)
        self.v_proj = nn.Linear(in_feats, num_heads * out_feats, bias=False)
        self.attn_drop = nn.Dropout(attn_drop)
        self.ln = nn.LayerNorm(in_feats)
        if trans:
            self.FFN = nn.Sequential(
                nn.Linear(in_feats, 4*in_feats),
                nn.PReLU(4*in_feats),
                nn.Linear(4*in_feats, in_feats),
                nn.Dropout(ffn_drop), # strange way, following the original code
            )
        #self.reset_parameters()
        self._trans = trans

    def reset_parameters(self):
        """Reinitialize learnable parameters."""
        gain = nn.init.calculate_gain('relu')
        for p in self.parameters():
            if p.ndim==2:
                nn.init.xavier_normal_(p, gain=gain)

    def forward(self, graph, feat):
        """Compute graph attention network layer.

        Parameters
        ----------
        graph : DGLGraph
            The graph.
        feat : torch.Tensor
            The input feature of shape :math:`(N, D_{in})` where :math:`D_{in}`
            is size of input feature, :math:`N` is the number of nodes.

        Returns
        -------
        torch.Tensor
            The output feature of shape :math:`(N, H, D_{out})` where :math:`H`
            is the number of heads, and :math:`D_{out}` is size of output feature.
        """
        graph = graph.local_var()
        #h = feat.view(-1, self._num_heads, self._out_feats)
        feat_c = feat.clone().detach().requires_grad_(False)
        q, k, v = self.q_proj(feat), self.k_proj(feat_c), self.v_proj(feat_c)
        q = q.view(-1, self._num_heads, self._out_feats)
        k = k.view(-1, self._num_heads, self._out_feats)
        v = v.view(-1, self._num_heads, self._out_feats)
        graph.ndata.update({'ft': q, 'el': k, 'er': v})
        # compute edge attention
        graph.apply_edges(fn.u_add_v('el', 'er', 'e'))
        e = math.sqrt(self._out_feats) * graph.edata.pop('e')
        # compute softmax
        graph.edata['a'] = self.attn_drop(edge_softmax(graph, e))
        # message passing
        graph.update_all(fn.u_mul_e('ft', 'a', 'm'),
                         fn.sum('m', 'ft'))
        rst = graph.ndata['ft']
        # residual
        rst = rst.view(feat.shape) + feat
        if self._trans:
            rst = self.ln(rst)
            rst = self.ln(rst+self.FFN(rst))
        return rst

class GraphTrans(nn.Module):
    def __init__(self,args):
        super().__init__()
        self.args = args
        if args.graph_enc == "gat":
            self.gat = nn.ModuleList([GAT(args.nhid, args.nhid//4, 4, attn_drop=args.attn_drop, trans=False) for _ in range(args.prop)]) #untested
        else:
            self.gat = nn.ModuleList([GAT(args.nhid, args.nhid//4, 4, attn_drop=args.attn_drop, ffn_drop=args.drop, trans=True) for _ in range(args.prop)])
        self.prop = args.prop

    def forward(self, ent, ent_mask, ent_len, rel, rel_mask, graphs):
        device = ent.device
#        if self.args.entdetach:
#            ent = ent.clone().detach()
        ent_mask = (ent_mask==0) # reverse mask
        rel_mask = (rel_mask==0)
        init_h = []
        for i in range(graphs.batch_size):
            init_h.append(ent[i][ent_mask[i]])
            init_h.append(rel[i][rel_mask[i]])
        init_h = torch.cat(init_h, 0)
        feats = init_h
        for i in range(self.prop):
            feats = self.gat[i](graphs, feats)
        g_root = feats.index_select(0, graphs.filter_nodes(lambda x: x.data['type']==NODE_TYPE['root']).to(device))
        g_ent = pad(feats.index_select(0, graphs.filter_nodes(lambda x: x.data['type']==NODE_TYPE['entity']).to(device)).split(ent_len), out_type='tensor')
        return g_ent, g_root