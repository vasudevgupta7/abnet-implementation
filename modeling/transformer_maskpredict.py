# __author__ = "Vasudev Gupta"

import os
import json

import torch
import torch.nn as nn
import torch.nn.functional as F
from transformers.file_utils import hf_bucket_url, cached_path

from modeling.adapters import MixAdapterTMP
from modeling.modeling_bert import BertModel
from modeling.decoding import MaskPredict
from modeling.utils import Dict


class TransformerMaskPredict(nn.Module, MaskPredict, MixAdapterTMP):

    def __init__(self, config):
        super().__init__()
        MixAdapterTMP.__init__(self)
        MaskPredict.__init__(self)

        self.config = config

        self.encoder = BertModel.from_pretrained(self.config["encoder_id"], num_lengths=self.config.num_lengths, add_length_embedding=True)
        self.decoder = BertModel.from_pretrained(self.config["decoder_id"])

        for param in self.encoder.parameters():
            param.requires_grad_(False)
        for param in self.decoder.parameters():
            param.requires_grad_(False)

        self.add_adapter_(True, True, True,
                        self.config.enc_ffn_adapter_config,
                        self.config.dec_ffn_adapter_config,
                        self.config.cross_attn_adapter_config)

        # now encoder will have ffn-adapter
        # decoder will have ffn-adapter & cross-attn-adapter

        self.adapter_requires_grad_(self.config.enc_ffn_adapter_requires_grad,
                                self.config.dec_ffn_adapter_requires_grad,
                                self.config.cross_attn_adapter_requires_grad)
        self.layers_requires_grad_(self.config.length_embed_requires_grad)

    def forward(self, input_ids, encoder_attention_mask, decoder_input_ids=None, decoder_attention_mask=None, labels=None, return_dict=True):
        """
        Input View:
            input_ids :: torch.tensor : [LENGTH], [CLS], ........., [SEP], [PAD] ...... [PAD]
            decoder_input_ids :: torch.tensor : [CLS], ........, [PAD] ...... [PAD]
            labels: torch.tensor : ............, [SEP], [PAD] ...... [PAD]
        """

        loss = None
        length_loss = None
        translation_loss = None

        # encoder
        x = self.encoder(input_ids=input_ids,
                    attention_mask=encoder_attention_mask,
                    return_dict=True)
        length_logits = x.pop("length_logits")
        x = torch.cat([length_logits, x.pop("last_hidden_state")], dim=1)

        # adding head over length logits
        length_logits = F.linear(length_logits, self.encoder.embeddings.length_embedding.weight, bias=None)
        # print(length_logits.shape)

        # decoder
        x = self.decoder(input_ids=decoder_input_ids,
                        attention_mask=decoder_attention_mask,
                        encoder_hidden_states=x,
                        encoder_attention_mask=encoder_attention_mask,
                        return_dict=True)
        x = x["last_hidden_state"]
        x = F.linear(x, self.decoder.embeddings.word_embeddings.weight, bias=None)

        if labels is not None:
            loss, length_loss, translation_loss = self.compute_loss(x, labels, length_logits)

        if not return_dict:
            return x, length_logits, loss, length_loss, translation_loss

        return {
            "logits": x,
            "length_logits": length_logits,
            "loss": loss,
            "length_loss": length_loss,
            "translation_loss": translation_loss
            }

    def compute_loss(self, final_logits, labels, length_logits, eps=0.1, reduction="sum"):
        # TODO
        # loss_fn = LossFunc(eps=eps, reduction=reduction)
        # return loss_fn(final_logits, labels, length_logits)
        final_logits = final_logits.view(-1, final_logits.size(-1))
        labels = labels.view(-1)
        return nn.CrossEntropyLoss()(final_logits, labels), None, None

    def save_pretrained(self, save_directory:str):
        """
            We are saving only the finetuned weights ; bert-weights in encoder and decoder are not getting saved 
            and can be loaded directly from huggingface hub
        """

        if save_directory not in os.listdir(): 
            os.makedirs(save_directory)

        # saving config
        path = os.path.join(save_directory, "config.json")
        with open(path, "w") as f:
            json.dump(self.config, f)

        # saving only the adapter weights and length embedding
        path = os.path.join(save_directory, "pytorch_model.bin")
        self.save_finetuned(path, print_status=False)

        return True

    @classmethod
    def from_pretrained(cls, pretrained_model_name_or_path:str):
        """
            Setting up this method will enable to load directly from huggingface hub just like other HF models are loaded
        """
        model_id = pretrained_model_name_or_path

        if len(model_id.split("/")) == 1:
            name = model_id
        else:
            username, name = model_id.split("/")

        if name in os.listdir():
            print("LOADING config & model weights from local directory")
            config_file = os.path.join(name, "config.json")
            model_file = os.path.join(name, "pytorch_model.bin")
        else:
            config_url = hf_bucket_url(model_id, filename="config.json")
            config_file = cached_path(config_url)
            # downloading & load only the adapter weights from huggingface hub
            # and corresponding bert weights will be loaded when class is getting initiated
            model_url = hf_bucket_url(model_id, filename="pytorch_model.bin")
            model_file = cached_path(model_url)

        with open(config_file, "r", encoding="utf-8") as f:
            config = json.load(f)
        config = Dict.from_nested_dict(config)

        state_dict = torch.load(model_file, map_location="cpu")
        # randomly initializing model from given config with bert weights restored
        model = cls(config)
        # now restoring adapter weights
        model.load_state_dict(state_dict, strict=False)
        model.eval()

        return model


class LossFunc(nn.Module):

    def __init__(self, eps=0.1, reduction="sum"):
        super().__init__()
        # TODO
        # think of padding
        self.eps = eps
        self.reduction = reduction

    # def compute_length_loss(self, length_logits, length_labels):
    #     length_logits = F.log_softmax(length_logits, dim=-1)
    #     length_loss = F.nll_loss(length_logits, length_labels, reduction=self.reduction)
    #     return length_loss

    # def compute_translation_loss(self, final_logits, labels):

    #     final_logits = F.log_softmax(final_logits, dim=-1)
    #     nll_loss = F.nll_loss(final_logits, labels, reduction=self.reduction)
    #     smooth_loss = final_logits.mean(-1)
        
    #     if self.reduction == "sum":
    #         smooth_loss = smooth_loss.sum()

    #     return (1.-self.eps)*nll_loss + self.eps*smooth_loss

    def compute_cross_entropy_loss(logits, targets, ignore_index=-100):
        """
        Function to compute the cross entropy loss. The default value of
        ignore_index is the same as the default value for F.cross_entropy in
        pytorch.
        """
        assert logits.size(0) == targets.size(-1), \
            "Logits and Targets tensor shapes don't match up"

        loss = F.nll_loss(
            F.log_softmax(logits, -1, dtype=torch.float32),
            targets,
            reduction="sum",
            ignore_index=ignore_index,
        )
        return loss

    def forward(self, final_logits, labels, length_logits):

        # TODO
        length_labels = labels.size(-1)

        # length_loss = self.compute_length_loss(length_logits, length_labels.size(-1))
        # translation_loss = self.compute_translation_loss(final_logits, labels)
        length_loss = None
        translation_loss = None
        loss = compute_cross_entropy_loss(length_logits, length_labels, self.padding_idx)
        ntokens = length_labels[length_labels.ne(self.padding_idx)].numel()
        loss = loss / ntokens

        # loss = 0.1*length_loss + translation_loss

        return {
            "loss": loss,
            "length_loss": length_loss,
            "translation_loss": translation_loss
            }
