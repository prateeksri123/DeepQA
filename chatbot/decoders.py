# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Library for creating sequence-to-sequence models in TensorFlow.
Sequence-to-sequence recurrent neural networks can learn complex functions
that map input sequences to output sequences. These models yield very good
results on a number of tasks, such as speech recognition, parsing, machine
translation, or even constructing automated replies to emails.
Before using this module, it is recommended to read the TensorFlow tutorial
on sequence-to-sequence models. It explains the basic concepts of this module
and shows an end-to-end example of how to build a translation model.
  https://www.tensorflow.org/versions/master/tutorials/seq2seq/index.html
Here is an overview of functions available in this module. They all use
a very similar interface, so after reading the above tutorial and using
one of them, others should be easy to substitute.
* Full sequence-to-sequence models.
  - basic_rnn_seq2seq: The most basic RNN-RNN model.
  - tied_rnn_seq2seq: The basic model with tied encoder and decoder weights.
  - embedding_rnn_seq2seq: The basic model with input embedding.
  - embedding_tied_rnn_seq2seq: The tied model with input embedding.
  - embedding_attention_seq2seq: Advanced model with input embedding and
      the neural attention mechanism; recommended for complex tasks.
* Multi-task sequence-to-sequence models.
  - one2many_rnn_seq2seq: The embedding model with multiple decoders.
* Decoders (when you write your own encoder, you can use these to decode;
    e.g., if you want to write a model that generates captions for images).
  - rnn_decoder: The basic decoder based on a pure RNN.
  - attention_decoder: A decoder that uses the attention mechanism.
* Losses.
  - sequence_loss: Loss for a sequence model returning average log-perplexity.
  - sequence_loss_by_example: As above, but not averaging over all examples.
* model_with_buckets: A convenience function to create models with bucketing
    (see the tutorial above for an explanation of why and how to use it).
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import copy

# We disable pylint because we need python3 compatibility.
from six.moves import xrange  # pylint: disable=redefined-builtin
from six.moves import zip  # pylint: disable=redefined-builtin

from tensorflow.contrib.rnn.python.ops import core_rnn
from tensorflow.contrib.rnn.python.ops import core_rnn_cell
from tensorflow.contrib.rnn.python.ops import core_rnn_cell_impl
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import ops
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import control_flow_ops
from tensorflow.python.ops import embedding_ops
from tensorflow.python.ops import math_ops
from tensorflow.python.ops import nn_ops
from tensorflow.python.ops import rnn
from tensorflow.python.ops import variable_scope
from tensorflow.python.util import nest
import tensorflow as tf

linear = core_rnn_cell_impl._linear  # pylint: disable=protected-access

'''
Code taken from https://github.com/pbhatia243/Neural_Conversation_Models
'''
def _extract_beam_search(embedding, beam_size, num_symbols, embedding_size,  output_projection=None,
                              update_embedding=True):
  """Get a loop_function that extracts the previous symbol and embeds it.
  Args:
    embedding: embedding tensor for symbols.
    output_projection: None or a pair (W, B). If provided, each fed previous
      output will first be multiplied by W and added B.
    update_embedding: Boolean; if False, the gradients will not propagate
      through the embeddings.
  Returns:
    A loop function.
  """
  def loop_function(prev, i, log_beam_probs, beam_path, beam_symbols):
    if output_projection is not None:
      prev = nn_ops.xw_plus_b(
          prev, output_projection[0], output_projection[1])

    probs  = tf.log(tf.nn.softmax(prev))

    if i > 1:

        probs = tf.reshape(probs + log_beam_probs[-1],
                               [-1, beam_size * num_symbols])

    best_probs, indices = tf.nn.top_k(probs, beam_size)
    indices = tf.stop_gradient(tf.squeeze(tf.reshape(indices, [-1, 1])))
    best_probs = tf.stop_gradient(tf.reshape(best_probs, [-1, 1]))

    symbols = indices % num_symbols # Which word in vocabulary.
    beam_parent = indices // num_symbols # Which hypothesis it came from.

    beam_symbols.append(symbols)
    beam_path.append(beam_parent)
    log_beam_probs.append(best_probs)

    # Note that gradients will not propagate through the second parameter of
    # embedding_lookup.

    emb_prev = embedding_ops.embedding_lookup(embedding, symbols)
    emb_prev  = tf.reshape(emb_prev,[beam_size,embedding_size])

    if not update_embedding:
      emb_prev = array_ops.stop_gradient(emb_prev)
    return emb_prev
  return loop_function

def _extract_argmax_and_embed(embedding,
                              output_projection=None,
                              update_embedding=True):
  """Get a loop_function that extracts the previous symbol and embeds it.
  Args:
    embedding: embedding tensor for symbols.
    output_projection: None or a pair (W, B). If provided, each fed previous
      output will first be multiplied by W and added B.
    update_embedding: Boolean; if False, the gradients will not propagate
      through the embeddings.
  Returns:
    A loop function.
  """

  def loop_function(prev, _):
    if output_projection is not None:
      prev = nn_ops.xw_plus_b(prev, output_projection[0], output_projection[1])
    prev_symbol = math_ops.argmax(prev, 1)
    # Note that gradients will not propagate through the second parameter of
    # embedding_lookup.
    emb_prev = embedding_ops.embedding_lookup(embedding, prev_symbol)
    if not update_embedding:
      emb_prev = array_ops.stop_gradient(emb_prev)
    return emb_prev

  return loop_function

def rnn_decoder(decoder_inputs,
                initial_state,
                cell,
                loop_function=None,
                scope=None):
  """RNN decoder for the sequence-to-sequence model.
  Args:
    decoder_inputs: A list of 2D Tensors [batch_size x input_size].
    initial_state: 2D Tensor with shape [batch_size x cell.state_size].
    cell: core_rnn_cell.RNNCell defining the cell function and size.
    loop_function: If not None, this function will be applied to the i-th output
      in order to generate the i+1-st input, and decoder_inputs will be ignored,
      except for the first element ("GO" symbol). This can be used for decoding,
      but also for training to emulate http://arxiv.org/abs/1506.03099.
      Signature -- loop_function(prev, i) = next
        * prev is a 2D Tensor of shape [batch_size x output_size],
        * i is an integer, the step number (when advanced control is needed),
        * next is a 2D Tensor of shape [batch_size x input_size].
    scope: VariableScope for the created subgraph; defaults to "rnn_decoder".
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x output_size] containing generated outputs.
      state: The state of each cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
        (Note that in some cases, like basic RNN cell or GRU cell, outputs and
         states can be the same. They are different for LSTM cells though.)
  """
  with variable_scope.variable_scope(scope or "rnn_decoder"):
    state = initial_state
    outputs = []
    prev = None
    for i, inp in enumerate(decoder_inputs):
      if loop_function is not None and prev is not None:
        with variable_scope.variable_scope("loop_function", reuse=True):
          inp = loop_function(prev, i)
      if i > 0:
        variable_scope.get_variable_scope().reuse_variables()
      output, state = cell(inp, state)
      outputs.append(output)
      if loop_function is not None:
        prev = output
  return outputs, state, None, None, None


def beam_rnn_decoder(decoder_inputs, initial_state, cell, loop_function=None,
                scope=None,output_projection=None, beam_size=10):
  """RNN decoder for the sequence-to-sequence model.
  Args:
    decoder_inputs: A list of 2D Tensors [batch_size x input_size].
    initial_state: 2D Tensor with shape [batch_size x cell.state_size].
    cell: rnn_cell.RNNCell defining the cell function and size.
    loop_function: If not None, this function will be applied to the i-th output
      in order to generate the i+1-st input, and decoder_inputs will be ignored,
      except for the first element ("GO" symbol). This can be used for decoding,
      but also for training to emulate http://arxiv.org/abs/1506.03099.
      Signature -- loop_function(prev, i) = next
        * prev is a 2D Tensor of shape [batch_size x output_size],
        * i is an integer, the step number (when advanced control is needed),
        * next is a 2D Tensor of shape [batch_size x input_size].
    scope: VariableScope for the created subgraph; defaults to "rnn_decoder".
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x output_size] containing generated outputs.
      state: The state of each cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
        (Note that in some cases, like basic RNN cell or GRU cell, outputs and
         states can be the same. They are different for LSTM cells though.)
  """
  with variable_scope.variable_scope(scope or "rnn_decoder"):
    state = initial_state
    outputs = []
    prev = None
    log_beam_probs, beam_path, beam_symbols = [],[],[]
    state_size = int(initial_state.get_shape().with_rank(2)[1])
    #state_size = cell.state_size
    print('state size', state_size)

    for i, inp in enumerate(decoder_inputs):
      if loop_function is not None and prev is not None:
        with variable_scope.variable_scope("loop_function", reuse=True):
          inp = loop_function(prev, i,log_beam_probs, beam_path, beam_symbols)
      if i > 0:
        variable_scope.get_variable_scope().reuse_variables()

      input_size = inp.get_shape().with_rank(2)[1]
      x = inp
      output, state = cell(x, state)

      if loop_function is not None:
        prev = output
      
      if i==0 and loop_function is not None:
          states =[]
          for kk in range(beam_size):
                states.append(state)
          state = tf.reshape(tf.concat(states, 0), [-1, state_size])
       
      if output_projection:
        outputs.append(tf.argmax(nn_ops.xw_plus_b(
          output, output_projection[0], output_projection[1]), dimension=1))
      else:
        outputs.append(output)
  if loop_function is not None:
    return outputs, state, tf.reshape(tf.concat(beam_path, 0),[-1,beam_size]), tf.reshape(tf.concat(beam_symbols, 0),[-1,beam_size]), tf.reshape(tf.concat(log_beam_probs, 0), [-1, beam_size])
  else:
    return outputs, state, None, None, None


def embedding_rnn_decoder(decoder_inputs, initial_state, cell, num_symbols,
                          embedding_size, output_projection=None,
                          feed_previous=False,
                          update_embedding_for_previous=True, scope=None, beam_search=True, beam_size=10 ):
  """RNN decoder with embedding and a pure-decoding option.
  Args:
    decoder_inputs: A list of 1D batch-sized int32 Tensors (decoder inputs).
    initial_state: 2D Tensor [batch_size x cell.state_size].
    cell: rnn_cell.RNNCell defining the cell function.
    num_symbols: Integer, how many symbols come into the embedding.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_symbols] and B has
      shape [num_symbols]; if provided and feed_previous=True, each fed
      previous output will first be multiplied by W and added B.
    feed_previous: Boolean; if True, only the first of decoder_inputs will be
      used (the "GO" symbol), and all other decoder inputs will be generated by:
        next = embedding_lookup(embedding, argmax(previous_output)),
      In effect, this implements a greedy decoder. It can also be used
      during training to emulate http://arxiv.org/abs/1506.03099.
      If False, decoder_inputs are used as given (the standard decoder case).
    update_embedding_for_previous: Boolean; if False and feed_previous=True,
      only the embedding for the first symbol of decoder_inputs (the "GO"
      symbol) will be updated by back propagation. Embeddings for the symbols
      generated from the decoder itself remain unchanged. This parameter has
      no effect if feed_previous=False.
    scope: VariableScope for the created subgraph; defaults to
      "embedding_rnn_decoder".
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x output_size] containing the generated outputs.
      state: The state of each decoder cell in each time-step. This is a list
        with length len(decoder_inputs) -- one item for each time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  Raises:
    ValueError: When output_projection has the wrong shape.
  """
  if output_projection is not None:
    proj_weights = ops.convert_to_tensor(output_projection[0],
                                         dtype=dtypes.float32)
    proj_weights.get_shape().assert_is_compatible_with([None, num_symbols])
    proj_biases = ops.convert_to_tensor(
        output_projection[1], dtype=dtypes.float32)
    proj_biases.get_shape().assert_is_compatible_with([num_symbols])

  with variable_scope.variable_scope(scope or "embedding_rnn_decoder"):
    with ops.device("/cpu:0"):
      embedding = variable_scope.get_variable("embedding",
                                              [num_symbols, embedding_size])

    if beam_search:
        loop_function = _extract_beam_search(
        embedding, beam_size,num_symbols,embedding_size,  output_projection,
        update_embedding_for_previous) if feed_previous else None
    else:
        loop_function = _extract_argmax_and_embed(
        embedding, output_projection,
        update_embedding_for_previous) if feed_previous else None

    emb_inp = [
        embedding_ops.embedding_lookup(embedding, i) for i in decoder_inputs]


    if beam_search:
        return beam_rnn_decoder(emb_inp, initial_state, cell,
                       loop_function=loop_function,output_projection=output_projection, beam_size=beam_size)

    else:
        return  rnn_decoder(emb_inp, initial_state, cell,
                       loop_function=loop_function)

def embedding_rnn_seq2seq(encoder_inputs, decoder_inputs, cell,
                          num_encoder_symbols, num_decoder_symbols,
                          embedding_size, output_projection=None,
                          feed_previous=False, dtype=dtypes.float32,
                          scope=None, beam_search=True, beam_size=10):
  """Embedding RNN sequence-to-sequence model.
  This model first embeds encoder_inputs by a newly created embedding (of shape
  [num_encoder_symbols x input_size]). Then it runs an RNN to encode
  embedded encoder_inputs into a state vector. Next, it embeds decoder_inputs
  by another newly created embedding (of shape [num_decoder_symbols x
  input_size]). Then it runs RNN decoder, initialized with the last
  encoder state, on embedded decoder_inputs.
  Args:
    encoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    decoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    cell: rnn_cell.RNNCell defining the cell function and size.
    num_encoder_symbols: Integer; number of symbols on the encoder side.
    num_decoder_symbols: Integer; number of symbols on the decoder side.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_decoder_symbols] and B has
      shape [num_decoder_symbols]; if provided and feed_previous=True, each
      fed previous output will first be multiplied by W and added B.
    feed_previous: Boolean or scalar Boolean Tensor; if True, only the first
      of decoder_inputs will be used (the "GO" symbol), and all other decoder
      inputs will be taken from previous outputs (as in embedding_rnn_decoder).
      If False, decoder_inputs are used as given (the standard decoder case).
    dtype: The dtype of the initial state for both the encoder and encoder
      rnn cells (default: tf.float32).
    scope: VariableScope for the created subgraph; defaults to
      "embedding_rnn_seq2seq"
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x num_decoder_symbols] containing the generated
        outputs.
      state: The state of each decoder cell in each time-step. This is a list
        with length len(decoder_inputs) -- one item for each time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope(scope or "embedding_rnn_seq2seq"):
    # Encoder.
    encoder_cell = core_rnn_cell.EmbeddingWrapper(
        cell, embedding_classes=num_encoder_symbols,
        embedding_size=embedding_size)
    _, encoder_state = core_rnn.static_rnn(encoder_cell, encoder_inputs, dtype=dtype)
    print(encoder_state)

    # Decoder.
    if output_projection is None:
      cell = core_rnn_cell.OutputProjectionWrapper(cell, num_decoder_symbols)


    return embedding_rnn_decoder(
          decoder_inputs, encoder_state, cell, num_decoder_symbols,
          embedding_size, output_projection=output_projection,
          feed_previous=feed_previous, beam_search=beam_search, beam_size=beam_size)

def beam_attention_decoder(decoder_inputs, 
                           initial_state, 
                           attention_states,
                           cell,
                           output_size=None,
                           num_heads=1,
                           loop_function=None,
                           dtype=None,
                           scope=None,
                           initial_state_attention=False,
                           first_step=False,
                           output_projection=None,
                           beam_size=10):
  """RNN decoder with attention for the sequence-to-sequence model.
  In this context "attention" means that, during decoding, the RNN can look up
  information in the additional tensor attention_states, and it does this by
  focusing on a few entries from the tensor. This model has proven to yield
  especially good results in a number of sequence-to-sequence tasks. This
  implementation is based on http://arxiv.org/abs/1412.7449 (see below for
  details). It is recommended for complex sequence-to-sequence tasks.
  Args:
    decoder_inputs: A list of 2D Tensors [batch_size x input_size].
    initial_state: 2D Tensor [batch_size x cell.state_size].
    attention_states: 3D Tensor [batch_size x attn_length x attn_size].
    cell: core_rnn_cell.RNNCell defining the cell function and size.
    output_size: Size of the output vectors; if None, we use cell.output_size.
    num_heads: Number of attention heads that read from attention_states.
    loop_function: If not None, this function will be applied to i-th output
      in order to generate i+1-th input, and decoder_inputs will be ignored,
      except for the first element ("GO" symbol). This can be used for decoding,
      but also for training to emulate http://arxiv.org/abs/1506.03099.
      Signature -- loop_function(prev, i) = next
        * prev is a 2D Tensor of shape [batch_size x output_size],
        * i is an integer, the step number (when advanced control is needed),
        * next is a 2D Tensor of shape [batch_size x input_size].
    dtype: The dtype to use for the RNN initial state (default: tf.float32).
    scope: VariableScope for the created subgraph; default: "attention_decoder".
    initial_state_attention: If False (default), initial attentions are zero.
      If True, initialize the attentions from the initial state and attention
      states -- useful when we wish to resume decoding from a previously
      stored decoder state and attention states.
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors of
        shape [batch_size x output_size]. These represent the generated outputs.
        Output i is computed from input i (which is either the i-th element
        of decoder_inputs or loop_function(output {i-1}, i)) as follows.
        First, we run the cell on a combination of the input and previous
        attention masks:
          cell_output, new_state = cell(linear(input, prev_attn), prev_state).
        Then, we calculate new attention masks:
          new_attn = softmax(V^T * tanh(W * attention_states + U * new_state))
        and then we calculate the output:
          output = linear(cell_output, new_attn).
      state: The state of each decoder cell the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  Raises:
    ValueError: when num_heads is not positive, there are no inputs, shapes
      of attention_states are not set, or input size cannot be inferred
      from the input.
  """
  if not decoder_inputs:
    raise ValueError("Must provide at least 1 input to attention decoder.")
  if num_heads < 1:
    raise ValueError("With less than 1 heads, use a non-attention decoder.")
  if attention_states.get_shape()[2].value is None:
    raise ValueError("Shape[2] of attention_states must be known: %s" %
                     attention_states.get_shape())
  if output_size is None:
    output_size = cell.output_size

  with variable_scope.variable_scope(
      scope or "attention_decoder", dtype=dtype) as scope:
    dtype = scope.dtype

    batch_size = array_ops.shape(decoder_inputs[0])[0]  # Needed for reshaping.
    attn_length = attention_states.get_shape()[1].value
    if attn_length is None:
      attn_length = array_ops.shape(attention_states)[1]
    attn_size = attention_states.get_shape()[2].value

    # To calculate W1 * h_t we use a 1-by-1 convolution, need to reshape before.
    hidden = array_ops.reshape(attention_states,
                               [-1, attn_length, 1, attn_size])
    hidden_features = []
    v = []
    attention_vec_size = attn_size  # Size of query vectors for attention.
    for a in xrange(num_heads):
      k = variable_scope.get_variable("AttnW_%d" % a,
                                      [1, 1, attn_size, attention_vec_size])
      hidden_features.append(nn_ops.conv2d(hidden, k, [1, 1, 1, 1], "SAME"))
      v.append(
          variable_scope.get_variable("AttnV_%d" % a, [attention_vec_size]))

    state = initial_state
    if loop_function is not None:
      state_size =  int(initial_state.get_shape().with_rank(2)[1])
      states=[]
      for kk in range(1):
        states.append(initial_state)
      state = tf.reshape(tf.concat(states, 0), [-1, state_size])

    def attention(query):
      """Put attention masks on hidden using hidden_features and query."""
      ds = []  # Results of attention reads will be stored here.
      if nest.is_sequence(query):  # If the query is a tuple, flatten it.
        query_list = nest.flatten(query)
        for q in query_list:  # Check that ndims == 2 if specified.
          ndims = q.get_shape().ndims
          if ndims:
            assert ndims == 2
        query = array_ops.concat(query_list, 1)
      for a in xrange(num_heads):
        with variable_scope.variable_scope("Attention_%d" % a):
          y = linear(query, attention_vec_size, True)
          y = array_ops.reshape(y, [-1, 1, 1, attention_vec_size])
          # Attention mask is a softmax of v^T * tanh(...).
          s = math_ops.reduce_sum(v[a] * math_ops.tanh(hidden_features[a] + y),
                                  [2, 3])
          a = nn_ops.softmax(s)
          # Now calculate the attention-weighted vector d.
          d = math_ops.reduce_sum(
              array_ops.reshape(a, [-1, attn_length, 1, 1]) * hidden, [1, 2])
          ds.append(array_ops.reshape(d, [-1, attn_size]))
      return ds

    outputs = []
    prev = None
    batch_attn_size = array_ops.stack([batch_size, attn_size])
    attns = [
        array_ops.zeros(
            batch_attn_size, dtype=dtype) for _ in xrange(num_heads)
    ]
    for a in attns:  # Ensure the second shape of attention vectors is set.
      a.set_shape([None, attn_size])
    if initial_state_attention:
      attns = attention(initial_state)
      if loop_function is not None:
        attns = []
        attns.append(attention(initial_state))
        tmp = tf.reshape(tf.concat(attns, 0), [-1, attn_size])
        attns = []
        attns.append(tmp)

    log_beam_probs, beam_path, beam_symbols = [],[],[]

    for i, inp in enumerate(decoder_inputs):
      if i > 0:
        variable_scope.get_variable_scope().reuse_variables()
      # If loop_function is set, we use it instead of decoder_inputs.
      if loop_function is not None and prev is not None:
        with variable_scope.variable_scope("loop_function", reuse=True):
          inp = loop_function(prev, i, log_beam_probs, beam_path, beam_symbols)
      # Merge input and previous attentions into one vector of the right size.
      input_size = inp.get_shape().with_rank(2)[1]
      if input_size.value is None:
        raise ValueError("Could not infer input size from input: %s" % inp.name)
      x = linear([inp] + attns, input_size, True)
      
      # Run the RNN.
      cell_output, state = cell(x, state)
      # Run the attention mechanism.
      if i == 0 and initial_state_attention:
        with variable_scope.variable_scope(
            variable_scope.get_variable_scope(), reuse=True):
          attns = attention(state)
      else:
        attns = attention(state)

      with variable_scope.variable_scope("AttnOutputProjection"):
        output = linear([cell_output] + attns, output_size, True)
      if loop_function is not None:
        prev = output
        if i == 0:
          states =[]
          for kk in range(beam_size):
            states.append(state)
          state = tf.reshape(tf.concat(states, 0), [-1, state_size])
          with variable_scope.variable_scope(variable_scope.get_variable_scope(), reuse=True):
            attns = attention(state)
        outputs.append(tf.argmax(nn_ops.xw_plus_b(
          output, output_projection[0], output_projection[1]), dimension=1))
      else:
        outputs.append(output)

  if loop_function is not None:
    return outputs, state, tf.reshape(tf.concat(beam_path, 0),[-1,beam_size]), tf.reshape(tf.concat(beam_symbols, 0),[-1,beam_size]), tf.reshape(tf.concat(log_beam_probs, 0), [-1, beam_size])
  else:
    return outputs, state, None, None, None

def beam_embedding_attention_decoder(decoder_inputs,
                                     initial_state,
                                     attention_states,
                                     cell,
                                     num_symbols,
                                     embedding_size,
                                     num_heads=1,
                                     output_size=None,
                                     output_projection=None,
                                     feed_previous=False,
                                     update_embedding_for_previous=True,
                                     dtype=None,
                                     scope=None,
                                     initial_state_attention=False,
                                     first_step=False,
                                     beam_search=True,
                                     beam_size=10):

  """RNN decoder with embedding and attention and a pure-decoding option.
  Args:
    decoder_inputs: A list of 1D batch-sized int32 Tensors (decoder inputs).
    initial_state: 2D Tensor [batch_size x cell.state_size].
    attention_states: 3D Tensor [batch_size x attn_length x attn_size].
    cell: core_rnn_cell.RNNCell defining the cell function.
    num_symbols: Integer, how many symbols come into the embedding.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    num_heads: Number of attention heads that read from attention_states.
    output_size: Size of the output vectors; if None, use output_size.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_symbols] and B has shape
      [num_symbols]; if provided and feed_previous=True, each fed previous
      output will first be multiplied by W and added B.
    feed_previous: Boolean; if True, only the first of decoder_inputs will be
      used (the "GO" symbol), and all other decoder inputs will be generated by:
        next = embedding_lookup(embedding, argmax(previous_output)),
      In effect, this implements a greedy decoder. It can also be used
      during training to emulate http://arxiv.org/abs/1506.03099.
      If False, decoder_inputs are used as given (the standard decoder case).
    update_embedding_for_previous: Boolean; if False and feed_previous=True,
      only the embedding for the first symbol of decoder_inputs (the "GO"
      symbol) will be updated by back propagation. Embeddings for the symbols
      generated from the decoder itself remain unchanged. This parameter has
      no effect if feed_previous=False.
    dtype: The dtype to use for the RNN initial states (default: tf.float32).
    scope: VariableScope for the created subgraph; defaults to
      "embedding_attention_decoder".
    initial_state_attention: If False (default), initial attentions are zero.
      If True, initialize the attentions from the initial state and attention
      states -- useful when we wish to resume decoding from a previously
      stored decoder state and attention states.
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x output_size] containing the generated outputs.
      state: The state of each decoder cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  Raises:
    ValueError: When output_projection has the wrong shape.
  """
  if output_size is None:
    output_size = cell.output_size
  if output_projection is not None:
    proj_biases = ops.convert_to_tensor(output_projection[1], dtype=dtype)
    proj_biases.get_shape().assert_is_compatible_with([num_symbols])

  with variable_scope.variable_scope(
      scope or "embedding_attention_decoder", dtype=dtype) as scope:

    embedding = variable_scope.get_variable("embedding",
                                            [num_symbols, embedding_size])

    if beam_search:
      loop_function = _extract_beam_search(
        embedding, beam_size,num_symbols, embedding_size, output_projection,
        update_embedding_for_previous) if feed_previous else None
    else:
      loop_function = _extract_argmax_and_embed(
        embedding, output_projection,
        update_embedding_for_previous) if feed_previous else None
    emb_inp = [
        embedding_ops.embedding_lookup(embedding, i) for i in decoder_inputs
    ]
    if beam_search:
      return beam_attention_decoder(
        emb_inp,
        initial_state,
        attention_states,
        cell,
        output_size=output_size,
        num_heads=num_heads,
        loop_function=loop_function,
        initial_state_attention=initial_state_attention,
        first_step=first_step,
        output_projection=output_projection,
        beam_size=beam_size)
    else:
      return attention_decoder(
            emb_inp, initial_state, attention_states, cell, output_size=output_size, num_heads=num_heads, loop_function=loop_function, initial_state_attention=initial_state_attention, output_projection=output_projection)

def embedding_attention_seq2seq(encoder_inputs,
                                decoder_inputs,
                                cell,
                                num_encoder_symbols,
                                num_decoder_symbols,
                                embedding_size,
                                num_heads=1,
                                output_projection=None,
                                feed_previous=False,
                                dtype=None,
                                scope=None,
                                initial_state_attention=False,
                                first_step=False,
                                beam_search=True,
                                beam_size=10):
  """Embedding sequence-to-sequence model with attention.
  This model first embeds encoder_inputs by a newly created embedding (of shape
  [num_encoder_symbols x input_size]). Then it runs an RNN to encode
  embedded encoder_inputs into a state vector. It keeps the outputs of this
  RNN at every step to use for attention later. Next, it embeds decoder_inputs
  by another newly created embedding (of shape [num_decoder_symbols x
  input_size]). Then it runs attention decoder, initialized with the last
  encoder state, on embedded decoder_inputs and attending to encoder outputs.
  Warning: when output_projection is None, the size of the attention vectors
  and variables will be made proportional to num_decoder_symbols, can be large.
  Args:
    encoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    decoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    cell: core_rnn_cell.RNNCell defining the cell function and size.
    num_encoder_symbols: Integer; number of symbols on the encoder side.
    num_decoder_symbols: Integer; number of symbols on the decoder side.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    num_heads: Number of attention heads that read from attention_states.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_decoder_symbols] and B has
      shape [num_decoder_symbols]; if provided and feed_previous=True, each
      fed previous output will first be multiplied by W and added B.
    feed_previous: Boolean or scalar Boolean Tensor; if True, only the first
      of decoder_inputs will be used (the "GO" symbol), and all other decoder
      inputs will be taken from previous outputs (as in embedding_rnn_decoder).
      If False, decoder_inputs are used as given (the standard decoder case).
    dtype: The dtype of the initial RNN state (default: tf.float32).
    scope: VariableScope for the created subgraph; defaults to
      "embedding_attention_seq2seq".
    initial_state_attention: If False (default), initial attentions are zero.
      If True, initialize the attentions from the initial state and attention
      states.
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x num_decoder_symbols] containing the generated
        outputs.
      state: The state of each decoder cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope(
      scope or "embedding_attention_seq2seq", dtype=dtype) as scope:
    dtype = scope.dtype
    # Encoder.
    encoder_cell = copy.deepcopy(cell)
    encoder_cell = core_rnn_cell.EmbeddingWrapper(
        encoder_cell,
        embedding_classes=num_encoder_symbols,
        embedding_size=embedding_size)
    encoder_outputs, encoder_state = core_rnn.static_rnn(
        encoder_cell, encoder_inputs, dtype=dtype)

    # First calculate a concatenation of encoder outputs to put attention on.
    top_states = [
        array_ops.reshape(e, [-1, 1, cell.output_size]) for e in encoder_outputs
    ]
    attention_states = array_ops.concat(top_states, 1)

    # Decoder.
    output_size = None
    if output_projection is None:
      cell = core_rnn_cell.OutputProjectionWrapper(cell, num_decoder_symbols)
      output_size = num_decoder_symbols

    if isinstance(feed_previous, bool):
      return beam_embedding_attention_decoder(
          decoder_inputs,
          encoder_state,
          attention_states,
          cell,
          num_decoder_symbols,
          embedding_size,
          num_heads=num_heads,
          output_size=output_size,
          output_projection=output_projection,
          feed_previous=feed_previous,
          initial_state_attention=initial_state_attention,
          first_step=first_step,
          beam_search=beam_search,
          beam_size=beam_size)

    # If feed_previous is a Tensor, we construct 2 graphs and use cond.
    def decoder(feed_previous_bool):
      reuse = None if feed_previous_bool else True
      with variable_scope.variable_scope(
          variable_scope.get_variable_scope(), reuse=reuse) as scope:
        outputs, state = embedding_attention_decoder(
            decoder_inputs,
            encoder_state,
            attention_states,
            cell,
            num_decoder_symbols,
            embedding_size,
            num_heads=num_heads,
            output_size=output_size,
            output_projection=output_projection,
            feed_previous=feed_previous_bool,
            update_embedding_for_previous=False,
            initial_state_attention=initial_state_attention,
            first_step=first_step)
        state_list = [state]
        if nest.is_sequence(state):
          state_list = nest.flatten(state)
        return outputs + state_list

    outputs_and_state = control_flow_ops.cond(feed_previous,
                                              lambda: decoder(True),
                                              lambda: decoder(False))
    outputs_len = len(decoder_inputs)  # Outputs length same as decoder inputs.
    state_list = outputs_and_state[outputs_len:]
    state = state_list[0]
    if nest.is_sequence(encoder_state):
      state = nest.pack_sequence_as(
          structure=encoder_state, flat_sequence=state_list)
    return outputs_and_state[:outputs_len], state


def attention_decoder_context(decoder_inputs,
                      decoder_context,
                      initial_state,
                      attention_states,
                      cell,
                      output_size=None,
                      num_heads=1,
                      loop_function=None,
                      dtype=None,
                      scope=None,
                      initial_state_attention=False,
                      first_step=False,
                      output_projection=None,
                      beam_search=True,
                      beam_size=10):
  """RNN decoder with attention for the sequence-to-sequence model.
  In this context "attention" means that, during decoding, the RNN can look up
  information in the additional tensor attention_states, and it does this by
  focusing on a few entries from the tensor. This model has proven to yield
  especially good results in a number of sequence-to-sequence tasks. This
  implementation is based on http://arxiv.org/abs/1412.7449 (see below for
  details). It is recommended for complex sequence-to-sequence tasks.
  Args:
    decoder_inputs: A list of 2D Tensors [batch_size x input_size].
    initial_state: 2D Tensor [batch_size x cell.state_size].
    attention_states: 3D Tensor [batch_size x attn_length x attn_size].
    cell: core_rnn_cell.RNNCell defining the cell function and size.
    output_size: Size of the output vectors; if None, we use cell.output_size.
    num_heads: Number of attention heads that read from attention_states.
    loop_function: If not None, this function will be applied to i-th output
      in order to generate i+1-th input, and decoder_inputs will be ignored,
      except for the first element ("GO" symbol). This can be used for decoding,
      but also for training to emulate http://arxiv.org/abs/1506.03099.
      Signature -- loop_function(prev, i) = next
        * prev is a 2D Tensor of shape [batch_size x output_size],
        * i is an integer, the step number (when advanced control is needed),
        * next is a 2D Tensor of shape [batch_size x input_size].
    dtype: The dtype to use for the RNN initial state (default: tf.float32).
    scope: VariableScope for the created subgraph; default: "attention_decoder".
    initial_state_attention: If False (default), initial attentions are zero.
      If True, initialize the attentions from the initial state and attention
      states -- useful when we wish to resume decoding from a previously
      stored decoder state and attention states.
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors of
        shape [batch_size x output_size]. These represent the generated outputs.
        Output i is computed from input i (which is either the i-th element
        of decoder_inputs or loop_function(output {i-1}, i)) as follows.
        First, we run the cell on a combination of the input and previous
        attention masks:
          cell_output, new_state = cell(linear(input, prev_attn), prev_state).
        Then, we calculate new attention masks:
          new_attn = softmax(V^T * tanh(W * attention_states + U * new_state))
        and then we calculate the output:
          output = linear(cell_output, new_attn).
      state: The state of each decoder cell the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  Raises:
    ValueError: when num_heads is not positive, there are no inputs, shapes
      of attention_states are not set, or input size cannot be inferred
      from the input.
  """
  if not decoder_inputs:
    raise ValueError("Must provide at least 1 input to attention decoder.")
  if num_heads < 1:
    raise ValueError("With less than 1 heads, use a non-attention decoder.")
  if attention_states.get_shape()[2].value is None:
    raise ValueError("Shape[2] of attention_states must be known: %s" %
                     attention_states.get_shape())
  if output_size is None:
    output_size = cell.output_size

  with variable_scope.variable_scope(
      scope or "attention_decoder", dtype=dtype) as scope:
    dtype = scope.dtype

    batch_size = array_ops.shape(decoder_inputs[0])[0]  # Needed for reshaping.
    attn_length = attention_states.get_shape()[1].value
    if attn_length is None:
      attn_length = array_ops.shape(attention_states)[1]
    attn_size = attention_states.get_shape()[2].value
    print('attn_size', attn_size)

    # To calculate W1 * h_t we use a 1-by-1 convolution, need to reshape before.
    hidden = array_ops.reshape(attention_states,
                               [-1, attn_length, 1, attn_size])
    hidden_features = []
    v = []
    attention_vec_size = attn_size  # Size of query vectors for attention.
    for a in xrange(num_heads):
      k = variable_scope.get_variable("AttnW_%d" % a,
                                      [1, 1, attn_size, attention_vec_size])
      hidden_features.append(nn_ops.conv2d(hidden, k, [1, 1, 1, 1], "SAME"))
      v.append(
          variable_scope.get_variable("AttnV_%d" % a, [attention_vec_size]))

    state = initial_state
    if loop_function is not None and beam_search:
      state_size =  int(initial_state.get_shape().with_rank(2)[1])
      states=[]
      for kk in range(1):
        states.append(initial_state)
        state = tf.reshape(tf.concat(states, 0), [-1, state_size])
        
    def attention(query):
      """Put attention masks on hidden using hidden_features and query."""
      ds = []  # Results of attention reads will be stored here.
      if nest.is_sequence(query):  # If the query is a tuple, flatten it.
        query_list = nest.flatten(query)
        for q in query_list:  # Check that ndims == 2 if specified.
          ndims = q.get_shape().ndims
          if ndims:
            assert ndims == 2
        query = array_ops.concat(query_list, 1)
      for a in xrange(num_heads):
        with variable_scope.variable_scope("Attention_%d" % a):
          y = linear(query, attention_vec_size, True)
          y = array_ops.reshape(y, [-1, 1, 1, attention_vec_size])
          # Attention mask is a softmax of v^T * tanh(...).
          s = math_ops.reduce_sum(v[a] * math_ops.tanh(hidden_features[a] + y),
                                  [2, 3])
          a = nn_ops.softmax(s)
          # Now calculate the attention-weighted vector d.
          d = math_ops.reduce_sum(
              array_ops.reshape(a, [-1, attn_length, 1, 1]) * hidden, [1, 2])
          ds.append(array_ops.reshape(d, [-1, attn_size]))
      return ds

    outputs = []
    prev = None
    batch_attn_size = array_ops.stack([batch_size, attn_size])
    attns = [
        array_ops.zeros(
            batch_attn_size, dtype=dtype) for _ in xrange(num_heads)
    ]
    print('num_heads', num_heads)
    for a in attns:  # Ensure the second shape of attention vectors is set.
      a.set_shape([None, attn_size])
    if initial_state_attention:
      attns = attention(initial_state)
      if loop_function is not None and beam_search:
        attns = []
        attns.append(attention(initial_state))
        tmp = tf.reshape(tf.concat(attns, 0), [-1, attn_size])
        attns = []
        attns.append(tmp)

    log_beam_probs, beam_path, beam_symbols = [],[],[]

    for i, (inp, context) in enumerate(zip(decoder_inputs, decoder_context)):
      if i > 0:
        variable_scope.get_variable_scope().reuse_variables()
      # If loop_function is set, we use it instead of decoder_inputs.
      if loop_function is not None and prev is not None:
        with variable_scope.variable_scope("loop_function", reuse=True):
          if beam_search:
            inp = loop_function(prev, i, log_beam_probs, beam_path, beam_symbols)
          else:
            inp = loop_function(prev, i)
      # Merge input and previous attentions into one vector of the right size.
      input_size = inp.get_shape().with_rank(2)[1]
      if input_size.value is None:
        raise ValueError("Could not infer input size from input: %s" % inp.name)

      # *** linearly combine embedded decoder input w/ food context vector ***
      #if (i == 0 and first_step) or (not first_step):
      #  x = linear([inp] + [context] + attns, input_size, True)
      
      if loop_function is not None and beam_search and i > 0:
        context_size = int(context.get_shape().with_rank(2)[1])
        context_list=[]
        #print('context orig shape', context.shape, context[0].shape)
        for kk in range(beam_size):
          context_list.append(context)
        context = tf.reshape(tf.concat(context_list, 1), [-1, context_size]) 
        #print('context shape', context.shape)
      #print('output size', output_size)
      x = linear([inp] + [context] + attns, input_size, True)
      
      # Run the RNN.
      cell_output, state = cell(x, state)
      # Run the attention mechanism.
      if i == 0 and initial_state_attention:
        with variable_scope.variable_scope(
            variable_scope.get_variable_scope(), reuse=True):
          attns = attention(state)
      else:
        attns = attention(state)

      with variable_scope.variable_scope("AttnOutputProjection"):
        output = linear([cell_output] + attns, output_size, True)
      if loop_function is not None:
        prev = output
        if i == 0 and beam_search:
          states = []
          for kk in range(beam_size):
            states.append(state)
          state = tf.reshape(tf.concat(states, 0), [-1, state_size])
          with variable_scope.variable_scope(variable_scope.get_variable_scope(), reuse=True):
            attns = attention(state)

      if loop_function is not None and beam_search:
        outputs.append(tf.argmax(nn_ops.xw_plus_b(
          output, output_projection[0], output_projection[1]), dimension=1))
      else:
        outputs.append(output)

    if loop_function is not None and beam_search:
      return outputs, state, tf.reshape(tf.concat(beam_path, 0),[-1,beam_size]), tf.reshape(tf.concat(beam_symbols, 0),[-1,beam_size]), tf.reshape(tf.concat(log_beam_probs, 0), [-1, beam_size])
    else:
      return outputs, state, None, None, None

def attention_decoder(decoder_inputs,
                      initial_state,
                      attention_states,
                      cell,
                      output_size=None,
                      num_heads=1,
                      loop_function=None,
                      dtype=None,
                      scope=None,
                      initial_state_attention=False,
                      output_projection=None):
  """RNN decoder with attention for the sequence-to-sequence model.
  In this context "attention" means that, during decoding, the RNN can look up
  information in the additional tensor attention_states, and it does this by
  focusing on a few entries from the tensor. This model has proven to yield
  especially good results in a number of sequence-to-sequence tasks. This
  implementation is based on http://arxiv.org/abs/1412.7449 (see below for
  details). It is recommended for complex sequence-to-sequence tasks.
  Args:
    decoder_inputs: A list of 2D Tensors [batch_size x input_size].
    initial_state: 2D Tensor [batch_size x cell.state_size].
    attention_states: 3D Tensor [batch_size x attn_length x attn_size].
    cell: core_rnn_cell.RNNCell defining the cell function and size.
    output_size: Size of the output vectors; if None, we use cell.output_size.
    num_heads: Number of attention heads that read from attention_states.
    loop_function: If not None, this function will be applied to i-th output
      in order to generate i+1-th input, and decoder_inputs will be ignored,
      except for the first element ("GO" symbol). This can be used for decoding,
      but also for training to emulate http://arxiv.org/abs/1506.03099.
      Signature -- loop_function(prev, i) = next
        * prev is a 2D Tensor of shape [batch_size x output_size],
        * i is an integer, the step number (when advanced control is needed),
        * next is a 2D Tensor of shape [batch_size x input_size].
    dtype: The dtype to use for the RNN initial state (default: tf.float32).
    scope: VariableScope for the created subgraph; default: "attention_decoder".
    initial_state_attention: If False (default), initial attentions are zero.
      If True, initialize the attentions from the initial state and attention
      states -- useful when we wish to resume decoding from a previously
      stored decoder state and attention states.
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors of
        shape [batch_size x output_size]. These represent the generated outputs.
        Output i is computed from input i (which is either the i-th element
        of decoder_inputs or loop_function(output {i-1}, i)) as follows.
        First, we run the cell on a combination of the input and previous
        attention masks:
          cell_output, new_state = cell(linear(input, prev_attn), prev_state).
        Then, we calculate new attention masks:
          new_attn = softmax(V^T * tanh(W * attention_states + U * new_state))
        and then we calculate the output:
          output = linear(cell_output, new_attn).
      state: The state of each decoder cell the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  Raises:
    ValueError: when num_heads is not positive, there are no inputs, shapes
      of attention_states are not set, or input size cannot be inferred
      from the input.
  """
  if not decoder_inputs:
    raise ValueError("Must provide at least 1 input to attention decoder.")
  if num_heads < 1:
    raise ValueError("With less than 1 heads, use a non-attention decoder.")
  if attention_states.get_shape()[2].value is None:
    raise ValueError("Shape[2] of attention_states must be known: %s" %
                     attention_states.get_shape())
  if output_size is None:
    output_size = cell.output_size

  with variable_scope.variable_scope(
      scope or "attention_decoder", dtype=dtype) as scope:
    dtype = scope.dtype

    batch_size = array_ops.shape(decoder_inputs[0])[0]  # Needed for reshaping.
    attn_length = attention_states.get_shape()[1].value
    if attn_length is None:
      attn_length = array_ops.shape(attention_states)[1]
    attn_size = attention_states.get_shape()[2].value

    # To calculate W1 * h_t we use a 1-by-1 convolution, need to reshape before.
    hidden = array_ops.reshape(attention_states,
                               [-1, attn_length, 1, attn_size])
    hidden_features = []
    v = []
    attention_vec_size = attn_size  # Size of query vectors for attention.
    for a in xrange(num_heads):
      k = variable_scope.get_variable("AttnW_%d" % a,
                                      [1, 1, attn_size, attention_vec_size])
      hidden_features.append(nn_ops.conv2d(hidden, k, [1, 1, 1, 1], "SAME"))
      v.append(
          variable_scope.get_variable("AttnV_%d" % a, [attention_vec_size]))

    state = initial_state

    def attention(query):
      """Put attention masks on hidden using hidden_features and query."""
      ds = []  # Results of attention reads will be stored here.
      if nest.is_sequence(query):  # If the query is a tuple, flatten it.
        query_list = nest.flatten(query)
        for q in query_list:  # Check that ndims == 2 if specified.
          ndims = q.get_shape().ndims
          if ndims:
            assert ndims == 2
        query = array_ops.concat(query_list, 1)
      for a in xrange(num_heads):
        with variable_scope.variable_scope("Attention_%d" % a):
          y = linear(query, attention_vec_size, True)
          y = array_ops.reshape(y, [-1, 1, 1, attention_vec_size])
          # Attention mask is a softmax of v^T * tanh(...).
          s = math_ops.reduce_sum(v[a] * math_ops.tanh(hidden_features[a] + y),
                                  [2, 3])
          a = nn_ops.softmax(s)
          # Now calculate the attention-weighted vector d.
          d = math_ops.reduce_sum(
              array_ops.reshape(a, [-1, attn_length, 1, 1]) * hidden, [1, 2])
          ds.append(array_ops.reshape(d, [-1, attn_size]))
      return ds

    outputs = []
    prev = None
    batch_attn_size = array_ops.stack([batch_size, attn_size])
    attns = [
        array_ops.zeros(
            batch_attn_size, dtype=dtype) for _ in xrange(num_heads)
    ]
    for a in attns:  # Ensure the second shape of attention vectors is set.
      a.set_shape([None, attn_size])
    if initial_state_attention:
      attns = attention(initial_state)
    for i, inp in enumerate(decoder_inputs):
      if i > 0:
        variable_scope.get_variable_scope().reuse_variables()
      # If loop_function is set, we use it instead of decoder_inputs.
      if loop_function is not None and prev is not None:
        with variable_scope.variable_scope("loop_function", reuse=True):
          inp = loop_function(prev, i)
      # Merge input and previous attentions into one vector of the right size.
      input_size = inp.get_shape().with_rank(2)[1]
      if input_size.value is None:
        raise ValueError("Could not infer input size from input: %s" % inp.name)
      x = linear([inp] + attns, input_size, True)
      # Run the RNN.
      cell_output, state = cell(x, state)
      # Run the attention mechanism.
      if i == 0 and initial_state_attention:
        with variable_scope.variable_scope(
            variable_scope.get_variable_scope(), reuse=True):
          attns = attention(state)
      else:
        attns = attention(state)

      with variable_scope.variable_scope("AttnOutputProjection"):
        output = linear([cell_output] + attns, output_size, True)
      if loop_function is not None:
        prev = output

      outputs.append(output)

  return outputs, state, None, None, None

def embedding_attention_decoder(decoder_inputs,
                                decoder_context,
                                initial_state,
                                attention_states,
                                cell,
                                num_symbols,
                                embedding_size,
                                num_heads=1,
                                output_size=None,
                                output_projection=None,
                                feed_previous=False,
                                update_embedding_for_previous=True,
                                dtype=None,
                                scope=None,
                                initial_state_attention=False,
                                first_step=False,
                                beam_search=True,
                                beam_size=10):
  """RNN decoder with embedding and attention and a pure-decoding option.
  Args:
    decoder_inputs: A list of 1D batch-sized int32 Tensors (decoder inputs).
    initial_state: 2D Tensor [batch_size x cell.state_size].
    attention_states: 3D Tensor [batch_size x attn_length x attn_size].
    cell: core_rnn_cell.RNNCell defining the cell function.
    num_symbols: Integer, how many symbols come into the embedding.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    num_heads: Number of attention heads that read from attention_states.
    output_size: Size of the output vectors; if None, use output_size.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_symbols] and B has shape
      [num_symbols]; if provided and feed_previous=True, each fed previous
      output will first be multiplied by W and added B.
    feed_previous: Boolean; if True, only the first of decoder_inputs will be
      used (the "GO" symbol), and all other decoder inputs will be generated by:
        next = embedding_lookup(embedding, argmax(previous_output)),
      In effect, this implements a greedy decoder. It can also be used
      during training to emulate http://arxiv.org/abs/1506.03099.
      If False, decoder_inputs are used as given (the standard decoder case).
    update_embedding_for_previous: Boolean; if False and feed_previous=True,
      only the embedding for the first symbol of decoder_inputs (the "GO"
      symbol) will be updated by back propagation. Embeddings for the symbols
      generated from the decoder itself remain unchanged. This parameter has
      no effect if feed_previous=False.
    dtype: The dtype to use for the RNN initial states (default: tf.float32).
    scope: VariableScope for the created subgraph; defaults to
      "embedding_attention_decoder".
    initial_state_attention: If False (default), initial attentions are zero.
      If True, initialize the attentions from the initial state and attention
      states -- useful when we wish to resume decoding from a previously
      stored decoder state and attention states.
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x output_size] containing the generated outputs.
      state: The state of each decoder cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  Raises:
    ValueError: When output_projection has the wrong shape.
  """
  if output_size is None:
    output_size = cell.output_size
  if output_projection is not None:
    proj_biases = ops.convert_to_tensor(output_projection[1], dtype=dtype)
    proj_biases.get_shape().assert_is_compatible_with([num_symbols])

  with variable_scope.variable_scope(
      scope or "embedding_attention_decoder", dtype=dtype) as scope:

    embedding = variable_scope.get_variable("embedding",
                                            [num_symbols, embedding_size])

    if beam_search:
      loop_function = _extract_beam_search(
        embedding, beam_size,num_symbols, embedding_size, output_projection,
        update_embedding_for_previous) if feed_previous else None
    else:
      loop_function = _extract_argmax_and_embed(
        embedding, output_projection,
        update_embedding_for_previous) if feed_previous else None
    emb_inp = [
        embedding_ops.embedding_lookup(embedding, i) for i in decoder_inputs
    ]
    return attention_decoder_context(
        emb_inp,
        decoder_context,
        initial_state,
        attention_states,
        cell,
        output_size=output_size,
        num_heads=num_heads,
        loop_function=loop_function,
        initial_state_attention=initial_state_attention,
        first_step=first_step,
        output_projection=output_projection,
        beam_search=beam_search,
        beam_size=beam_size)


def embedding_attention_context_seq2seq(encoder_inputs,
                                decoder_inputs,
                                decoder_context,
                                cell,
                                num_encoder_symbols,
                                num_decoder_symbols,
                                embedding_size,
                                num_heads=1,
                                output_projection=None,
                                feed_previous=False,
                                dtype=None,
                                scope=None,
                                initial_state_attention=False,
                                first_step=False,
                                beam_search=True,
                                beam_size=10):
  """Embedding sequence-to-sequence model with attention.
  This model first embeds encoder_inputs by a newly created embedding (of shape
  [num_encoder_symbols x input_size]). Then it runs an RNN to encode
  embedded encoder_inputs into a state vector. It keeps the outputs of this
  RNN at every step to use for attention later. Next, it embeds decoder_inputs
  by another newly created embedding (of shape [num_decoder_symbols x
  input_size]). Then it runs attention decoder, initialized with the last
  encoder state, on embedded decoder_inputs and attending to encoder outputs.
  Warning: when output_projection is None, the size of the attention vectors
  and variables will be made proportional to num_decoder_symbols, can be large.
  Args:
    encoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    decoder_inputs: A list of 1D int32 Tensors of shape [batch_size].
    cell: core_rnn_cell.RNNCell defining the cell function and size.
    num_encoder_symbols: Integer; number of symbols on the encoder side.
    num_decoder_symbols: Integer; number of symbols on the decoder side.
    embedding_size: Integer, the length of the embedding vector for each symbol.
    num_heads: Number of attention heads that read from attention_states.
    output_projection: None or a pair (W, B) of output projection weights and
      biases; W has shape [output_size x num_decoder_symbols] and B has
      shape [num_decoder_symbols]; if provided and feed_previous=True, each
      fed previous output will first be multiplied by W and added B.
    feed_previous: Boolean or scalar Boolean Tensor; if True, only the first
      of decoder_inputs will be used (the "GO" symbol), and all other decoder
      inputs will be taken from previous outputs (as in embedding_rnn_decoder).
      If False, decoder_inputs are used as given (the standard decoder case).
    dtype: The dtype of the initial RNN state (default: tf.float32).
    scope: VariableScope for the created subgraph; defaults to
      "embedding_attention_seq2seq".
    initial_state_attention: If False (default), initial attentions are zero.
      If True, initialize the attentions from the initial state and attention
      states.
  Returns:
    A tuple of the form (outputs, state), where:
      outputs: A list of the same length as decoder_inputs of 2D Tensors with
        shape [batch_size x num_decoder_symbols] containing the generated
        outputs.
      state: The state of each decoder cell at the final time-step.
        It is a 2D Tensor of shape [batch_size x cell.state_size].
  """
  with variable_scope.variable_scope(
      scope or "embedding_attention_seq2seq", dtype=dtype) as scope:
    dtype = scope.dtype
    # Encoder.
    encoder_cell = copy.deepcopy(cell)
    encoder_cell = core_rnn_cell.EmbeddingWrapper(
        encoder_cell,
        embedding_classes=num_encoder_symbols,
        embedding_size=embedding_size)
    encoder_outputs, encoder_state = core_rnn.static_rnn(
        encoder_cell, encoder_inputs, dtype=dtype)

    # First calculate a concatenation of encoder outputs to put attention on.
    top_states = [
        array_ops.reshape(e, [-1, 1, cell.output_size]) for e in encoder_outputs
    ]
    attention_states = array_ops.concat(top_states, 1)

    # Decoder.
    output_size = None
    if output_projection is None:
      cell = core_rnn_cell.OutputProjectionWrapper(cell, num_decoder_symbols)
      output_size = num_decoder_symbols

    if isinstance(feed_previous, bool):
      return embedding_attention_decoder(
          decoder_inputs,
          decoder_context,
          encoder_state,
          attention_states,
          cell,
          num_decoder_symbols,
          embedding_size,
          num_heads=num_heads,
          output_size=output_size,
          output_projection=output_projection,
          feed_previous=feed_previous,
          initial_state_attention=initial_state_attention,
          first_step=first_step,
          beam_search=beam_search,
          beam_size=beam_size)

    # If feed_previous is a Tensor, we construct 2 graphs and use cond.
    def decoder(feed_previous_bool):
      reuse = None if feed_previous_bool else True
      with variable_scope.variable_scope(
          variable_scope.get_variable_scope(), reuse=reuse) as scope:
        outputs, state = embedding_attention_decoder(
            decoder_inputs,
            decoder_context,
            encoder_state,
            attention_states,
            cell,
            num_decoder_symbols,
            embedding_size,
            num_heads=num_heads,
            output_size=output_size,
            output_projection=output_projection,
            feed_previous=feed_previous_bool,
            update_embedding_for_previous=False,
            initial_state_attention=initial_state_attention,
            first_step=first_step)
        state_list = [state]
        if nest.is_sequence(state):
          state_list = nest.flatten(state)
        return outputs + state_list

    outputs_and_state = control_flow_ops.cond(feed_previous,
                                              lambda: decoder(True),
                                              lambda: decoder(False))
    outputs_len = len(decoder_inputs)  # Outputs length same as decoder inputs.
    state_list = outputs_and_state[outputs_len:]
    state = state_list[0]
    if nest.is_sequence(encoder_state):
      state = nest.pack_sequence_as(
          structure=encoder_state, flat_sequence=state_list)
    return outputs_and_state[:outputs_len], state

