import functools
import math
from typing import Optional, Tuple, Any, Union, List, Dict, Mapping
import chex
import flax.linen.partitioning
import jax.lax
from transformers.modeling_flax_outputs import FlaxBaseModelOutput, FlaxCausalLMOutput, FlaxMaskedLMOutput, ModelOutput
from flax.core import FrozenDict, freeze, unfreeze
from flax.linen import combine_masks
from flax.traverse_util import unflatten_dict, flatten_dict
from jax import numpy as jnp, lax
from flax import linen as nn
from .rwkv_configuration import RwkvConfig
from ..easydel_modelling_utils import EasyDelFlaxPretrainedModel
from jax.sharding import PartitionSpec
from flax.struct import dataclass


@dataclass
class RwkvOutput(ModelOutput):
    last_hidden_state: chex.Array = None
    state: Optional[Tuple[chex.Array, ...]] = None
    hidden_states: Optional[Tuple[chex.Array, ...]] = None
    attentions: Optional[Tuple[chex.Array, ...]] = None


@dataclass
class RwkvCausalLMOutput(ModelOutput):
    logits: chex.Array = None
    state: Optional[List[chex.Array]] = None
    hidden_states: Optional[Tuple[chex.Array, ...]] = None
    attentions: Optional[Tuple[chex.Array, ...]] = None


def init_to_value(x, dtype):
    return lambda _: x.astype(dtype)


@jax.jit
def rwkv_linear_attention(time_decay, time_first, key, value, state=None, return_state=False):
    current_sequence_length = key.shape[1]
    output = jnp.zeros_like(key)

    if state is None:
        num_state = jnp.zeros_like(key[:, 0], dtype=jnp.float32)
        den_state = jnp.zeros_like(key[:, 0], dtype=jnp.float32)
        max_state = jnp.zeros_like(key[:, 0], dtype=jnp.float32) - 1e38
    else:
        num_state, den_state, max_state = state

    time_decay = -jnp.exp(time_decay)

    for current_index in range(current_sequence_length):
        current_key = key[:, current_index].float()
        current_value = value[:, current_index]

        max_for_output = jnp.maximum(max_state, current_key + time_first)
        e1 = jnp.exp(max_state - max_for_output)
        e2 = jnp.exp(current_key + time_first - max_for_output)
        numerator = e1 * num_state + e2 * current_value
        denominator = e1 * den_state + e2
        output[:, current_index] = (numerator / denominator).astype(output.dtype)

        max_for_state = jnp.maximum(max_state + time_decay, current_key)
        e1 = jnp.exp(max_state + time_decay - max_for_state)
        e2 = jnp.exp(current_key - max_for_state)
        num_state = e1 * num_state + e2 * current_value
        den_state = e1 * den_state + e2
        max_state = max_for_state

    if return_state or state is not None:
        state = [num_state, den_state, max_state]

    return output, state


class FlaxRwkvSelfAttention(nn.Module):
    config: RwkvConfig
    layer_id: int
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[Union[str, jax.lax.Precision]] = None

    def setup(self) -> None:
        config = self.config
        hidden_size = config.hidden_size
        num_hidden_layers = config.num_hidden_layers
        layer_id = self.layer_id
        hidden_size = self.config.hidden_size
        attention_hidden_size = (
            config.attention_hidden_size if config.attention_hidden_size is not None else hidden_size
        )
        self.attention_hidden_size = attention_hidden_size

        ratio_0_to_1 = layer_id / (num_hidden_layers - 1)
        ratio_1_to_almost_0 = 1.0 - (layer_id / num_hidden_layers)
        zigzag = .5 * (jnp.arange(1, hidden_size + 1) % 3 - 1)
        time_first = jnp.full(hidden_size, math.log(.3)) + zigzag
        h = jnp.arange(0, hidden_size)
        time_decay = -5 + 8 * (h / (hidden_size - 1)) ** (.7 + 1.3 * ratio_0_to_1)
        x = jnp.arange(hidden_size) / hidden_size

        time_mix_key = jnp.power(x, ratio_1_to_almost_0)
        time_mix_value = time_mix_key + .3 * ratio_0_to_1
        time_mix_receptance = jnp.power(x, .5 * ratio_1_to_almost_0)

        self.time_decay = self.param(
            "time_decay",
            init_fn=init_to_value(time_decay, self.dtype),
        )
        self.time_first = self.param(
            "time_first",
            init_fn=init_to_value(time_first, self.dtype),
        )
        self.time_mix_key = self.param(
            "time_mix_key",
            init_fn=init_to_value(time_mix_key, self.dtype),
        )
        self.time_mix_value = self.param(
            "time_mix_value",
            init_fn=init_to_value(time_mix_value, self.dtype),
        )
        self.time_mix_receptance = self.param(
            "time_mix_receptance",
            init_fn=init_to_value(time_mix_receptance, self.dtype),
        )

        self.key = nn.Dense(
            attention_hidden_size,
            use_bias=False,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.value = nn.Dense(
            attention_hidden_size,
            use_bias=False,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.receptance = nn.Dense(
            attention_hidden_size,
            use_bias=False,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.output = nn.Dense(
            hidden_size,
            use_bias=False,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )

    def extract_key_value(self, hidden, state=None):
        if hidden.shape[1] == 1 and state is not None:
            shifted = state[1][:, :, self.layer_id]
        else:
            shifted = jnp.pad(
                hidden,
                pad_width=((0, 0), (1, 0), (0, 1)),
                mode="constant",
                constant_values=0
            )
            if state is not None:
                shifted[:, 0] = state[1][:, :, self.layer_id]
        key = hidden * self.time_mix_key + shifted * (1 - self.time_mix_key)
        value = hidden * self.time_mix_value + shifted * (1 - self.time_mix_value)
        receptance = hidden * self.time_mix_receptance + shifted * (1 - self.time_mix_receptance)

        key = self.key(key)
        value = self.value(value)
        receptance = jax.nn.sigmoid(
            self.receptance(
                receptance
            )
        )
        if state is not None:
            state[1][:, :, self.layer_id] = hidden[:, -1]
        return receptance, key, value, state

    def __call__(
            self,
            hidden,
            state=None,
            use_cache=False
    ):
        receptance, key, value, state = self.extract_key_value(hidden, state=state)
        layer_state = tuple(s[:, :, self.layer_id] for s in state[2:]) if state is not None else None
        rwkv, layer_state = rwkv_linear_attention(
            self.time_decay,
            self.time_first,
            key,
            value,
            state=layer_state,
            return_state=use_cache,
        )

        if layer_state is not None:
            state[2][:, :, self.layer_id] = layer_state[0]
            state[3][:, :, self.layer_id] = layer_state[1]
            state[4][:, :, self.layer_id] = layer_state[2]

        return self.output(receptance * rwkv), state


class FlaxRwkvFeedForward(nn.Module):
    config: RwkvConfig
    layer_id: int
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[Union[str, jax.lax.Precision]] = None

    def setup(self):
        config = self.config
        hidden_size = config.hidden_size
        layer_id = self.layer_id
        num_hidden_layers = self.config.num_hidden_layers
        intermediate_size = (
            config.intermediate_size if config.intermediate_size is not None else 4 * config.hidden_size
        )

        x = jnp.arange(hidden_size) / hidden_size

        ratio_1_to_almost_0 = 1.0 - (layer_id / num_hidden_layers)
        time_mix_key = jnp.power(x, ratio_1_to_almost_0)
        time_mix_receptance = jnp.power(x, .5 * ratio_1_to_almost_0)

        self.time_mix_key = self.param(
            "time_mix_key",
            init_fn=init_to_value(time_mix_key, self.param_dtype)
        )
        self.time_mix_receptance = self.param(
            "time_mix_receptance",
            init_fn=init_to_value(time_mix_receptance, self.param_dtype)
        )

        self.key = nn.Dense(
            intermediate_size,
            use_bias=False,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.receptance = nn.Dense(
            hidden_size,
            use_bias=False,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.value = nn.Dense(
            hidden_size,
            use_bias=False,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )

    def __call__(
            self,
            hidden,
            state=None
    ):
        if hidden.shape[1] == 1 and state is not None:
            shifted = state[0][:, :, self.layer_id]
        else:
            shifted = jnp.pad(
                hidden,
                pad_width=((0, 0), (0, 0), (1, 0), (0, 1)),
                mode='constant',
                constant_values=0
            )
            if state is not None:
                shifted[:, 0] = state[0][:, :, self.layer_id]
        key = hidden * self.time_mix_key + shifted * (1 - self.time_mix_key)
        receptance = hidden * self.time_mix_receptance + shifted * (1 - self.time_mix_receptance)

        key = jnp.square(jax.nn.relu(self.key(key)))
        value = self.value(key)
        receptance = jax.nn.sigmoid(self.receptance(receptance))

        if state is not None:
            state[0][:, :, self.layer_id] = hidden[:, -1]

        return receptance * value, state


class FlaxRwkvBlock(nn.Module):
    config: RwkvConfig
    layer_id: int
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[Union[str, jax.lax.Precision]] = None

    def setup(self):

        config = self.config
        layer_id = self.layer_id

        if layer_id == 0:
            self.pre_ln = nn.LayerNorm(
                epsilon=config.layer_norm_epsilon,
                dtype=self.dtype,
                param_dtype=self.param_dtype,
            )

        self.ln1 = nn.LayerNorm(
            epsilon=config.layer_norm_epsilon,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
        )
        self.ln2 = nn.LayerNorm(
            epsilon=config.layer_norm_epsilon,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
        )

        self.attention = FlaxRwkvSelfAttention(
            config=config,
            layer_id=layer_id,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.feed_forward = FlaxRwkvFeedForward(
            config=config,
            layer_id=layer_id,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )

    def __call__(
            self,
            hidden,
            state=None,
            use_cache: bool = False,
            output_attentions: bool = False
    ):
        if self.layer_id == 0:
            hidden = self.pre_ln(hidden)

        attention, state = self.attention(
            self.ln1(hidden),
            state=state,
            use_cache=use_cache
        )
        hidden = hidden + attention

        feed_forward, state = self.feed_forward(
            self.ln2(hidden),
            state=state
        )
        hidden = hidden + feed_forward

        outputs = (hidden, state)
        if output_attentions:
            outputs += (attention,)
        else:
            outputs += (None,)

        return outputs


class FlaxRwkvBlockCollection(nn.Module):
    config: RwkvConfig
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[Union[str, jax.lax.Precision]] = None

    def setup(self) -> None:
        self.blocks = [
            FlaxRwkvBlock(
                config=self.config,
                dtype=self.dtype,
                param_dtype=self.param_dtype,
                precision=self.precision,
                layer_id=idx,
                name=str(idx)
            )
            for idx in range(self.config.num_hidden_layers)
        ]

        self.layers_are_rescaled = False

    def __call__(
            self,
            hidden_states: chex.Array,
            attention_mask: Optional[chex.Array] = None,
            state: Optional[List[chex.Array]] = None,
            use_cache: Optional[bool] = None,
            deterministic: Optional[bool] = True,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = None,
    ):
        all_hidden_states = ()
        all_self_attentions = ()
        use_cache = use_cache if use_cache is not None else (self.config.use_cache if not deterministic else False)
        for idx, block in enumerate(self.blocks):

            hidden_states, state, attentions = block(
                hidden_states, state=state, use_cache=use_cache, output_attentions=output_attentions
            )

            if (
                    self.layers_are_rescaled
                    and self.config.rescale_every > 0
                    and (idx + 1) % self.config.rescale_every == 0
            ):
                hidden_states = hidden_states / 2

            if output_hidden_states:
                all_hidden_states = all_hidden_states + (hidden_states,)

            if output_attentions:
                all_self_attentions = all_self_attentions + (attentions,)
        return hidden_states, all_hidden_states, all_self_attentions


class FlaxRwkvModule(nn.Module):
    config: RwkvConfig
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[Union[str, jax.lax.Precision]] = None

    def setup(self):
        config = self.config
        self.embeddings = nn.Embed(
            config.vocab_size,
            config.hidden_size,
            dtype=self.dtype,
            param_dtype=self.param_dtype
        )
        self.blocks = FlaxRwkvBlockCollection(
            config=self.config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )

        self.ln_out = nn.LayerNorm(
            dtype=self.dtype,
            param_dtype=self.param_dtype,
        )

    def __call__(
            self,
            input_ids: Optional[chex.Array] = None,
            attention_mask: Optional[chex.Array] = None,
            inputs_embeds: Optional[chex.Array] = None,
            state: Optional[List[chex.Array]] = None,
            deterministic: bool = True,
            use_cache: Optional[bool] = None,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = None,
    ) -> Union[Tuple, RwkvOutput]:
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        use_cache = use_cache if use_cache is not None else (self.config.use_cache if not deterministic else False)
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        if input_ids is not None and inputs_embeds is not None:
            raise ValueError("You cannot specify both input_ids and inputs_embeds at the same time")
        elif input_ids is None and inputs_embeds is None:
            raise ValueError("You have to specify either input_ids or inputs_embeds")

        if inputs_embeds is None:
            inputs_embeds = self.embeddings(input_ids)

        if use_cache and state is None:
            shape = (inputs_embeds.shape[0], self.config.hidden_size, self.config.num_hidden_layers)
            state = [
                jnp.zeros(
                    *shape, dtype=inputs_embeds.dtype if i <= 1 else jnp.float32,
                )
                for i in range(5)
            ]
            state[4] -= 1e30

        hidden_states = inputs_embeds

        hidden_states, all_hidden_states, all_self_attentions = self.blocks(
            hidden_states,
            attention_mask,
            state,
            use_cache,
            deterministic,
            output_attentions,
            output_hidden_states,
            return_dict,
        )

        hidden_states = self.ln_out(hidden_states)

        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        if not return_dict:
            return tuple(x for x in [hidden_states, state, all_hidden_states, all_self_attentions] if x is not None)

        return RwkvOutput(
            last_hidden_state=hidden_states,
            state=state,
            hidden_states=all_hidden_states,
            attentions=all_self_attentions,
        )


class FlaxRwkvForCausalLMModule(nn.Module):
    config: RwkvConfig
    dtype: jnp.dtype = jnp.float32
    param_dtype: jnp.dtype = jnp.float32
    precision: Optional[Union[str, jax.lax.Precision]] = None

    def setup(self):
        config = self.config
        self.rwkv = FlaxRwkvModule(
            config,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )
        self.head = nn.Dense(
            config.vocab_size,
            use_bias=False,
            dtype=self.dtype,
            param_dtype=self.param_dtype,
            precision=self.precision
        )

    def __call__(
            self,
            input_ids: Optional[chex.Array] = None,
            attention_mask: Optional[chex.Array] = None,
            inputs_embeds: Optional[chex.Array] = None,
            state: Optional[List[chex.Array]] = None,
            deterministic: bool = True,
            use_cache: Optional[bool] = None,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = None,
    ) -> Union[Tuple, RwkvCausalLMOutput]:
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict

        rwkv_outputs = self.rwkv(
            input_ids,
            inputs_embeds=inputs_embeds,
            state=state,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
            deterministic=deterministic
        )
        hidden_states = rwkv_outputs[0]

        logits = self.head(hidden_states)

        if not return_dict:
            return (logits,) + rwkv_outputs[1:]

        return RwkvCausalLMOutput(
            logits=logits,
            state=rwkv_outputs.state,
            hidden_states=rwkv_outputs.hidden_states,
            attentions=rwkv_outputs.attentions,
        )


class FlaxRwkvPretrainedModel(EasyDelFlaxPretrainedModel):
    module_class: nn.Module
    config_class = RwkvConfig

    def __init__(
            self,
            config: RwkvConfig,
            input_shape: Tuple = (1, 1),
            seed: int = 0,
            dtype: jnp.dtype = jnp.float32,
            param_dtype: jnp.dtype = jnp.float32,
            precision: Optional[Union[jax.lax.Precision, str]] = None,
            _do_init: bool = True,
            **kwargs
    ):
        super().__init__(
            config=config,
            module=self.module_class(
                config=config,
                dtype=dtype,
                param_dtype=param_dtype,
                precision=precision,
                **kwargs
            ),
            input_shape=input_shape,
            seed=seed,
            dtype=dtype,
            _do_init=_do_init
        )

    def init_weights(
            self,
            rng: jax.random.PRNGKey,
            input_shape: Tuple,
            params: FrozenDict = None) -> FrozenDict[Any, Any] | Mapping[str, Any] | Any:
        input_ids = jnp.zeros(input_shape, dtype="i4")
        attention_mask = jnp.ones_like(input_ids)
        params_rng, dropout_rng = jax.random.split(rng)
        rng_s = {"params": params_rng, "dropout": dropout_rng}
        module_init_outputs = self.module.init(
            rng_s, input_ids, attention_mask, return_dict=False
        )

        random_params = module_init_outputs["params"]

        if params is not None:
            random_params = flatten_dict(unfreeze(random_params))
            params = flatten_dict(unfreeze(params))
            for missing_key in self._missing_keys:
                params[missing_key] = random_params[missing_key]
            self._missing_keys = set()
            return freeze(unflatten_dict(params))
        else:
            return random_params

    def __call__(
            self,
            input_ids: Optional[chex.Array] = None,
            attention_mask: Optional[chex.Array] = None,
            inputs_embeds: Optional[chex.Array] = None,
            state: Optional[List[chex.Array]] = None,
            use_cache: Optional[bool] = None,
            output_attentions: Optional[bool] = None,
            output_hidden_states: Optional[bool] = None,
            return_dict: Optional[bool] = None,
            params: dict = None,
            dropout_rng: jax.random.PRNGKey = None,
            train: bool = False,
            extra_embedding: Optional[Union[jnp.ndarray, None]] = None,
            add_params_field: bool = False,
    ):
        output_attentions = output_attentions if output_attentions is not None else self.config.output_attentions
        output_hidden_states = (
            output_hidden_states if output_hidden_states is not None else self.config.output_hidden_states
        )
        return_dict = return_dict if return_dict is not None else self.config.return_dict

        batch_size, sequence_length = input_ids.shape

        if attention_mask is None:
            attention_mask = jnp.ones((batch_size, sequence_length))

        rng_s = {}
        if dropout_rng is not None:
            rng_s["dropout"] = dropout_rng

        inputs = {
            "params": params or self.params} if add_params_field else params or self.params

        if self.config.bits is not None:
            rng_s['params'] = jax.random.key(0)

        mutable = False

        return self.module.apply(
            inputs,
            input_ids,
            attention_mask,
            inputs_embeds,
            state,
            use_cache,
            train,
            output_attentions,
            output_hidden_states,
            return_dict,
            rngs=rng_s,
            mutable=mutable,
        )

    def generate(self, *args, **kwargs):
        try:
            gen_output = super().generate(*args, **kwargs)
        except AttributeError as exc:
            if "past_key_values" in str(exc):
                raise AttributeError(
                    "You tried to call `generate` with a decoding strategy that manipulates `past_key_values`. RWKV "
                    "doesn't have that attribute, try another generation strategy instead. For the available "
                    "generation strategies, check this doc:"
                    " https://huggingface.co/docs/transformers/en/generation_strategies#decoding-strategies"
                )
            else:
                raise exc
        return gen_output

    def prepare_inputs_for_generation(self, input_ids, state=None, inputs_embeds=None, **kwargs):
        if state is not None:
            input_ids = input_ids[:, -1].unsqueeze(-1)
        if inputs_embeds is not None and state is None:
            model_inputs = {"inputs_embeds": inputs_embeds}
        else:
            model_inputs = {"input_ids": input_ids}

        model_inputs["state"] = state
        return model_inputs


class FlaxRwkvModel(FlaxRwkvPretrainedModel):
    module_class = FlaxRwkvModule


class FlaxRwkvForCausalLM(FlaxRwkvPretrainedModel):
    module_class = FlaxRwkvForCausalLMModule