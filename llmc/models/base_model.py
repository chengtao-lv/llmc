import gc
from abc import ABCMeta, abstractmethod
from collections import defaultdict
from functools import partial

import torch
import torch.nn as nn
from loguru import logger
from torch.nn import functional as F
from transformers import AutoConfig, AutoModelForCausalLM
from transformers.feature_extraction_utils import BatchFeature

from llmc.compression.quantization.module_utils import (
    _LLMC_LINEAR_TYPES_, _LLMC_LN_TYPES_, _TRANSFORMERS_LINEAR_TYPES_,
    _TRANSFORMERS_LN_TYPES_)
from llmc.compression.quantization.utils import (check_do_quant, check_w_only,
                                                 get_aquantizer,
                                                 get_wquantizer)


class BaseModel(metaclass=ABCMeta):
    def __init__(self, model_path, torch_dtype, device_map=None, use_cache=False):
        self.model_path = model_path
        self.torch_dtype = torch_dtype if torch_dtype == 'auto' else eval(torch_dtype)
        self.device_map = device_map
        self.use_cache = use_cache
        self.vlm_model = None
        self.build_model()
        self.model.eval()
        self.find_blocks()
        self.find_encoder_blocks()
        self.find_embed_layers()
        self.find_block_name()
        self.add_layernorms_class()

    @abstractmethod
    def find_blocks(self):
        pass

    def find_encoder_blocks(self):
        pass

    def get_encoder_catcher(self, first_block_input):
        pass

    def find_block_name(self):
        pass

    def get_model(self):
        return self.model

    def get_blocks(self, modality="language"):
        return self.blocks if modality=="language" else self.encoder_blocks

    @abstractmethod
    def find_embed_layers(self):
        pass

    @abstractmethod
    def get_embed_layers(self):
        pass

    @abstractmethod
    def get_layers_except_blocks(self):
        pass

    def get_matmul_in_block(self):
        return {}

    def get_act_fn_in_block(self):
        return {}

    def get_softmax_in_block(self):
        return {}

    def get_linears_except_subsets(self, block):
        return {}

    @abstractmethod
    def get_subsets_in_block(self, block):
        pass

    @abstractmethod
    def skip_layer_name(self):
        pass

    @abstractmethod
    def has_bias(self):
        pass

    def get_attention_rotary_layers(self):
        return []

    def batch_process(self):
        raise Exception('batch_process should not be called here.')

    def get_catcher(self, first_block_input):

        class Catcher(nn.Module):
            def __init__(self, module):
                super().__init__()
                self.module = module

            def forward(self, inp, **kwargs):
                first_block_input['data'].append(inp)
                if 'output_router_logits' in kwargs:
                    assert kwargs['output_router_logits'] is False
                    kwargs.pop('output_router_logits')
                first_block_input['kwargs'].append(kwargs)
                raise ValueError

        return Catcher

    def __str__(self):
        return f'\nConfig: \n{str(self.model_config)} \nModel: \n{str(self.model)}'

    def build_model(self):
        self.model_config = AutoConfig.from_pretrained(
            self.model_path, trust_remote_code=True
        )
        if not self.use_cache:
            if hasattr(self.model_config, 'use_cache'):
                self.model_config.use_cache = False
        logger.info(f'self.model_config : {self.model_config}')
        self.model = AutoModelForCausalLM.from_pretrained(
            self.model_path,
            config=self.model_config,
            device_map=self.device_map,
            trust_remote_code=True,
            torch_dtype=self.torch_dtype,
            low_cpu_mem_usage=True,
        )

    def add_layernorms_class(self):
        ln_class_list = []
        single_block = self.get_blocks()[0]
        ln_dict = self.get_layernorms_in_block(single_block)
        for ln_name in ln_dict:
            ln_class = ln_dict[ln_name].__class__
            if ln_class not in ln_class_list:
                ln_class_list.append(ln_class)
        for ln_class in ln_class_list:
            if ln_class not in _TRANSFORMERS_LN_TYPES_:
                _TRANSFORMERS_LN_TYPES_.append(ln_class)
        logger.info(f'_TRANSFORMERS_LN_TYPES_ : {_TRANSFORMERS_LN_TYPES_}')

    @torch.no_grad()
    def collect_first_block_input(self, calib_data, padding_mask=None, padding_side=None, data_type='txt'):  # noqa
        first_block_input = defaultdict(list)

        Catcher = self.get_catcher(first_block_input)

        self.move_embed_to_device('cuda')
        if data_type == 'img_txt':
            self.vision_model = self.vision_model.to('cuda')
            self.projector = self.projector.to('cuda')
        self.blocks[0] = self.blocks[0].cuda()
        self.blocks[0] = Catcher(self.blocks[0])

        for data in calib_data:
            if isinstance(data, BatchFeature):
                data = data.to(next(self.model.parameters()).device)
            else:
                data = {
                    k: (v.to(next(self.model.parameters()).device) if torch.is_tensor(v) else v)
                    for k, v in data.items()
                }
            try:
                if data_type in ['txt', 'img']:
                    self.model(**data)
                elif data_type == 'img_txt':
                    self.vlm_model.generate(**data, max_new_tokens=128, do_sample=False)
            except ValueError:
                pass
        self.first_block_input = first_block_input
        if padding_mask:
            for idx in range(len(self.first_block_input['data'])):
                token_num = self.first_block_input['data'][idx].shape[1]
                if token_num != padding_mask[idx].shape[1]:
                    padding_mask[idx] = F.pad(
                        padding_mask[idx],
                        self.get_one_pad_setting(
                            padding_side,
                            token_num - padding_mask[idx].shape[1]
                        ),
                        value=1
                    )
        self.padding_mask = padding_mask
        if data_type == 'img_txt':
            self.vision_model = self.vision_model.cpu()
            self.projector = self.projector.cpu()
        self.blocks[0] = self.blocks[0].module
        self.blocks[0] = self.blocks[0].cpu()
        self.move_embed_to_device('cpu')

    @torch.no_grad()
    def collect_first_encoder_block_input(self, calib_data, padding_mask=None, padding_side=None, data_type='txt'):  # noqa
        first_block_input = defaultdict(list)

        Catcher = self.get_encoder_catcher(first_block_input)

        self.move_embed_to_device('cuda')
        if data_type == 'img_txt':
            self.vision_model = self.vision_model.to('cuda')
            self.projector = self.projector.to('cuda')
        self.encoder_blocks[0] = self.encoder_blocks[0].cuda()
        self.encoder_blocks[0] = Catcher(self.encoder_blocks[0])

        for data in calib_data:
            if isinstance(data, BatchFeature):
                data = data.to(next(self.model.parameters()).device)
            else:
                data = {
                    k: (v.to(next(self.model.parameters()).device) if torch.is_tensor(v) else v)
                    for k, v in data.items()
                }
            try:
                if data_type in ['txt', 'img']:
                    self.model(**data)
                elif data_type == 'img_txt':
                    self.vlm_model.generate(**data, max_new_tokens=128, do_sample=False)
            except ValueError:
                pass
        self.first_block_input = first_block_input
        self.padding_mask = None
        if data_type == 'img_txt':
            self.vision_model = self.vision_model.cpu()
            self.projector = self.projector.cpu()
        self.encoder_blocks[0] = self.encoder_blocks[0].module
        self.encoder_blocks[0] = self.encoder_blocks[0].cpu()
        self.move_embed_to_device('cpu')

    def get_one_pad_setting(self, padding_side, length):
        if padding_side == 'left':
            return [0, length]
        elif padding_side == 'right':
            return [length, 0]
        else:
            raise Exception(f'Not support padding_side: {padding_side}.')

    def get_first_block_input(self):
        return self.first_block_input

    def get_padding_mask(self):
        return self.padding_mask

    def get_model_config(self):
        return self.model_config

    def move_embed_to_device(self, device):
        for embed_layer in self.get_embed_layers():
            embed_layer.to(device)
        for attention_rotary_layer in self.get_attention_rotary_layers():
            attention_rotary_layer.to(device)

    def get_block_linears(self, block):
        return {
            name: m
            for name, m in block.named_modules()
            if isinstance(m, tuple(_LLMC_LINEAR_TYPES_ + _TRANSFORMERS_LINEAR_TYPES_))
        }

    def get_extra_modules(self, block):
        return {}

    def set_mix_bits_params_dict(self, block_idx, name, params_dict):

        logger.info('set_mix_bits_params_dict')

        if not check_do_quant(
            block_idx,
            name,
            params_dict['mix_bits_map'],
            params_dict['quantizer_mix_bits'],
        ):
            logger.info(
                f'This layer {name} in {block_idx}-th block is set to float.'
                'No need to replace this layer.'
            )
            return params_dict

        params_mix_dict = {}
        params_mix_dict['debug_print'] = {}
        wquantizer = get_wquantizer(
            block_idx,
            name,
            params_dict['mix_bits_map'],
            params_dict['quantizer_mix_bits'],
            params_dict['wquantizer_default'],
        )
        params_mix_dict['w_qdq'] = partial(params_dict['w_qdq'], wquantizer=wquantizer)
        params_mix_dict['debug_print']['weight'] = {}
        params_mix_dict['debug_print']['weight']['bit'] = wquantizer.bit
        params_mix_dict['debug_print']['weight']['sym'] = wquantizer.sym
        params_mix_dict['debug_print']['weight']['granularity'] = wquantizer.granularity
        if wquantizer.granularity == 'per_group':
            params_mix_dict['debug_print']['weight'][
                'group_size'
            ] = wquantizer.group_size
        if not check_w_only(
            block_idx,
            name,
            params_dict['mix_bits_map'],
            params_dict['quantizer_mix_bits'],
            params_dict['w_only_default'],
        ):
            aquantizer = get_aquantizer(
                block_idx,
                name,
                params_dict['mix_bits_map'],
                params_dict['quantizer_mix_bits'],
                params_dict['aquantizer_default'],
            )
            params_mix_dict['a_qdq'] = partial(
                params_dict['a_qdq'], aquantizer=aquantizer
            )
            params_mix_dict['debug_print']['act'] = {}
            params_mix_dict['debug_print']['act']['bit'] = aquantizer.bit
            params_mix_dict['debug_print']['act']['sym'] = aquantizer.sym
            params_mix_dict['debug_print']['act'][
                'granularity'
            ] = aquantizer.granularity
        else:
            params_mix_dict['a_qdq'] = None
        return params_mix_dict

    def replace_modality_module_all(self, module, blocks, params_dict, keep_device=False):
        for block_idx in range(len(blocks)):
            logger.info(f'Replace block index: {block_idx}/{len(blocks)}')
            block = blocks[block_idx]
            if keep_device:
                self.replace_module_block(module, block, block_idx, params_dict)
            else:
                block = block.cuda()
                self.replace_module_block(module, block, block_idx, params_dict)
                block = block.cpu()

    def replace_module_all(self, module, params_dict, keep_device=False):
        if hasattr(self, "encoder_blocks"):
            logger.info("start replace vision blocks")
            self.replace_modality_module_all(module, self.encoder_blocks, params_dict, keep_device)
        
        logger.info("start replace language blocks")
        self.replace_modality_module_all(module, self.blocks, params_dict, keep_device)

        gc.collect()
        torch.cuda.empty_cache()
        logger.info(f'The Replaced model: {self.model}')

    def replace_module_block(self, module, block, block_idx, params_dict):
        if module in _LLMC_LN_TYPES_ + _TRANSFORMERS_LN_TYPES_:
            layer_norms = self.get_layernorms_in_block(block)
            self.replace_module_layernorm(
                module, block, layer_norms, block_idx, params_dict
            )
        else:
            subset = {}
            subset['layers'] = self.get_block_linears(block)
            self.replace_module_subset(module, block, subset, block_idx, params_dict)

    def replace_module_subset(self, module, block, subset, block_idx, params_dict):
        layers_dict = subset['layers']

        for name, m in layers_dict.items():
            # mix bits
            params_tmp_dict = {}
            if 'mix_bits' in params_dict and params_dict['mix_bits']:
                params_tmp_dict = self.set_mix_bits_params_dict(
                    block_idx, name, params_dict
                )
            else:
                params_tmp_dict = params_dict

            M = module.new(m, **params_tmp_dict)

            name_tmp = name.rsplit('.', 1)
            if len(name_tmp) == 2:
                parent_name = name_tmp[0]
                parent = block.get_submodule(parent_name)
                child_name = name_tmp[1]
            elif len(name_tmp) == 1:
                parent = block
                child_name = name_tmp[0]

            setattr(parent, child_name, M)
            logger.info(f'replace >>> {name} in {block_idx}-th block')

    def replace_module_layernorm(self, module, block, lns, i, params_dict):
        for name, m in lns.items():
            if isinstance(m, module):
                continue
            M = module.new(m, **params_dict)

            name_tmp = name.rsplit('.', 1)
            if len(name_tmp) == 2:
                parent_name = name_tmp[0]
                parent = block.get_submodule(parent_name)
                child_name = name_tmp[1]
            elif len(name_tmp) == 1:
                parent = block
                child_name = name_tmp[0]

            setattr(parent, child_name, M)

    def convert_dtype(self, dtype='torch.float16'):
        for i in range(len(self.blocks)):
            self.blocks[i] = self.blocks[i].to(dtype)
