# Copyright 2015 Conchylicultor. All Rights Reserved.
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

"""
Main script. See README.md for more information

Use python 3.5 virtualenv: source py3.5env/bin/activate
Use Cuda 8.0

Train: python main.py --corpus healthy-comments
--attention 1 -> uses attention RNN decoder
--food_context 1 -> adds sum of food embeddings as context to RNN decoder
--first_step 1 -> limits food context to only first step of decoder
--augment 1 -> augments food context w/ context vector of nearest neighbor foods
-- healthy_flag 1 -> append word "healthy"/"unhealthy"
--motivate_only 1 -> use only motivational data
--advice_only 1 -> use only advice data

Test: python main.py --corpus healthy-comments --test interactive
--beam_search 0 -> use greedy search instead of beam search
--beam_size 100 -> keep 100 candidate responses instead of only 10
--all_data 1 -> use models trained on all data instead of 90% training data

With --corpus nutrition:
--encode_food_descrips 1 -> uses USDA food description as input (not meal)
--encode_food_ids 1 -> uses USDA food ID as input
--encode_single_food_descrip 1 -> uses one USDA food description as input
--match_encoder_decoder_input 1 -> uses same input as encoder for decoder

Evaluate with BLEU:

python main.py --corpus healthy-comments --test all
./multi-bleu.perl reference < output

"""

import argparse  # Command line parsing
import configparser  # Saving the models parameters
import datetime  # Chronometer
import os  # Files management
from tqdm import tqdm  # Progress bar
import tensorflow as tf
import csv
import nltk
import math
import operator
import json

from chatbot.textdata import TextData
from chatbot.model import Model
from chatbot.healthydata import load_usda_vecs, HealthyData


class Chatbot:
    """
    Main class which launch the training or testing mode
    """

    class TestMode:
        """ Simple structure representing the different testing modes
        """
        ALL = 'all'
        INTERACTIVE = 'interactive'  # The user can write his own questions
        DAEMON = 'daemon'  # The chatbot runs on background and can regularly be called to predict something

    def __init__(self):
        """
        """
        # Model/dataset parameters
        self.args = None

        # Task specific object
        self.textData = None  # Dataset
        self.model = None  # Sequence to sequence model

        # Tensorflow utilities for convenience saving/logging
        self.writer = None
        self.saver = None
        self.modelDir = ''  # Where the model is saved
        self.globStep = 0  # Represent the number of iteration for the current model

        # TensorFlow main session (we keep track for the daemon)
        self.sess = None

        # Filename and directories constants
        self.MODEL_DIR_BASE = 'save/model'
        self.MODEL_NAME_BASE = 'model'
        self.MODEL_EXT = '.ckpt'
        self.CONFIG_FILENAME = 'params.ini'
        self.CONFIG_VERSION = '0.3'
        self.TEST_IN_NAME = 'data/test/samples.txt'
        self.TEST_OUT_SUFFIX = '_predictions.txt'
        self.REFERENCES_SUFFIX = '_reference.txt'
        self.SENTENCES_PREFIX = ['Q: ', 'A: ']

    @staticmethod
    def parseArgs(args):
        """
        Parse the arguments from the given command line
        Args:
            args (list<str>): List of arguments to parse. If None, the default sys.argv will be parsed
        """

        parser = argparse.ArgumentParser()

        # Global options
        globalArgs = parser.add_argument_group('Global options')
        globalArgs.add_argument('--test',
                                nargs='?',
                                choices=[Chatbot.TestMode.ALL, Chatbot.TestMode.INTERACTIVE, Chatbot.TestMode.DAEMON],
                                const=Chatbot.TestMode.ALL, default=None,
                                help='if present, launch the program try to answer all sentences from data/test/ with'
                                     ' the defined model(s), in interactive mode, the user can wrote his own sentences,'
                                     ' use daemon mode to integrate the chatbot in another program')
        globalArgs.add_argument('--createDataset', action='store_true', help='if present, the program will only generate the dataset from the corpus (no training/testing)')
        globalArgs.add_argument('--playDataset', type=int, nargs='?', const=10, default=None,  help='if set, the program  will randomly play some samples(can be use conjointly with createDataset if this is the only action you want to perform)')
        globalArgs.add_argument('--reset', action='store_true', help='use this if you want to ignore the previous model present on the model directory (Warning: the model will be destroyed with all the folder content)')
        globalArgs.add_argument('--verbose', action='store_true', help='When testing, will plot the outputs at the same time they are computed')
        globalArgs.add_argument('--keepAll', action='store_true', help='If this option is set, all saved model will be keep (Warning: make sure you have enough free disk space or increase saveEvery)')  # TODO: Add an option to delimit the max size
        globalArgs.add_argument('--modelTag', type=str, default=None, help='tag to differentiate which model to store/load')
        globalArgs.add_argument('--rootDir', type=str, default=None, help='folder where to look for the models and data')
        globalArgs.add_argument('--watsonMode', action='store_true', help='Inverse the questions and answer when training (the network try to guess the question)')
        globalArgs.add_argument('--device', type=str, default=None, help='\'gpu\' or \'cpu\' (Warning: make sure you have enough free RAM), allow to choose on which hardware run the model')
        globalArgs.add_argument('--seed', type=int, default=None, help='random seed for replication')

        # Dataset options
        datasetArgs = parser.add_argument_group('Dataset options')
        datasetArgs.add_argument('--corpus', type=str, default='cornell', help='corpus on which extract the dataset: cornell or nutrition or healthy-comment')
        datasetArgs.add_argument('--healthy_flag', type=int, default=0, help='whether to append healthy/unhealthy flag at end of input meal')
        datasetArgs.add_argument('--encode_food_descrips', type=int, default=0, help='whether to encode food descriptions')
        datasetArgs.add_argument('--encode_food_ids', type=int, default=0, help='whether to encode food descriptions')
        datasetArgs.add_argument('--encode_single_food_descrip', type=int, default=0, help='whether to encode single food descriptions')
        datasetArgs.add_argument('--match_encoder_decoder_input', type=int, default=0, help='whether to use same input for encoder and decoder')
        datasetArgs.add_argument('--motivate_only', type=int, default=0, help='only use the first AMT response, the motivational support')
        datasetArgs.add_argument('--advice_only', type=int, default=0, help='only use the 2nd AMT response, the advice part')
        datasetArgs.add_argument('--datasetTag', type=str, default=None, help='add a tag to the dataset (file where to load the vocabulary and the precomputed samples, not the original corpus). Useful to manage multiple versions')  # The samples are computed from the corpus if it does not exist already. There are saved in \'data/samples/\'
        datasetArgs.add_argument('--ratioDataset', type=float, default=1.0, help='ratio of dataset used to avoid using the whole dataset')  # Not implemented, useless ?
        datasetArgs.add_argument('--maxLength', type=int, default=10, help='maximum length of the sentence (for input and output), define number of maximum step of the RNN')
        datasetArgs.add_argument('--augment', type=int, default=0, help='whether to include additional meals with similar foods')
        datasetArgs.add_argument('--finetune', type=int, default=0, help='whether to continue training on nutrition data')
        datasetArgs.add_argument('--all_data', type=int, default=0, help='whether to use the full model trained on all data')

        # Network options (Warning: if modifying something here, also make the change on save/loadParams() )
        nnArgs = parser.add_argument_group('Network options', 'architecture related option')
        nnArgs.add_argument('--hiddenSize', type=int, default=50, help='number of hidden units in each RNN cell')
        nnArgs.add_argument('--numLayers', type=int, default=1, help='number of rnn layers')
        nnArgs.add_argument('--embeddingSize', type=int, default=64, help='embedding size of the word representation')
        nnArgs.add_argument('--softmaxSamples', type=int, default=0, help='Number of samples in the sampled softmax loss function. A value of 0 deactivates sampled softmax')
        nnArgs.add_argument('--attention', type=int, default=0, help='whether to use RNN with attention')
        nnArgs.add_argument('--food_context', type=int, default=0, help='whether to use decoder with food context vec')
        nnArgs.add_argument('--first_step', type=int, default=0, help='whether to limit food context vec to first decode step and input zeros for the rest')
        nnArgs.add_argument('--beam_search', type=int, default=1, help='whether to decode using beam search')
        nnArgs.add_argument('--beam_size', type=int, default=10, help='number of candidate paths to keep on beam during beam search decode')
        nnArgs.add_argument('--MMI', type=int, default=0, help='whether to rank decoded candidates with MMI criterion')
        nnArgs.add_argument('--lambda_wt', type=float, default=0.1, help='weight controlling how much to penalize target response in final MMI score')
        nnArgs.add_argument('--gamma_wt', type=int, default=1, help='number words in target to penalize/weight for length term of MMI score')
        
        # Training options
        trainingArgs = parser.add_argument_group('Training options')
        trainingArgs.add_argument('--numEpochs', type=int, default=30, help='maximum number of epochs to run')
        trainingArgs.add_argument('--saveEvery', type=int, default=1000, help='nb of mini-batch step before creating a model checkpoint')
        trainingArgs.add_argument('--batchSize', type=int, default=10, help='mini-batch size')
        trainingArgs.add_argument('--learningRate', type=float, default=0.001, help='Learning rate')

        return parser.parse_args(args)

    def main(self, args=None):
        """
        Launch the training and/or the interactive mode
        """
        print('Welcome to DeepQA v0.1 !')
        print()
        print('TensorFlow detected: v{}'.format(tf.__version__))

        # General initialisation

        self.args = self.parseArgs(args)
        if self.args.corpus == 'nutrition':
            self.args.maxLength = 100
            if self.args.encode_food_descrips:
                self.MODEL_DIR_BASE = 'save/food-meal-model'
                self.SENTENCES_PREFIX = ['Input food: ', 'Output meal: ']
            elif self.args.encode_single_food_descrip:
                self.MODEL_DIR_BASE = 'save/single-food-meal-model'
                self.SENTENCES_PREFIX = ['Input food: ', 'Output meal: ']
            elif self.args.encode_food_ids:
                self.MODEL_DIR_BASE = 'save/foodID-meal-model'
                self.SENTENCES_PREFIX = ['Input food: ', 'Output meal: ']
            else:
                self.MODEL_DIR_BASE = 'save/meal-model'
                self.SENTENCES_PREFIX = ['Input meal: ', 'Output meal: ']

        elif self.args.corpus == 'healthy-comments':
            self.args.maxLength = 100
            self.args.usda_vecs = load_usda_vecs()
            if self.args.all_data:
                self.MODEL_DIR_BASE = 'save_allData/healthy-comments'
            else:
                self.MODEL_DIR_BASE = 'save/healthy-comments'
            if self.args.healthy_flag:
                self.MODEL_DIR_BASE = 'save/healthy-comments-flag'
            elif self.args.encode_food_ids:
                self.MODEL_DIR_BASE = 'save/healthy-comments-foodID'
            self.SENTENCES_PREFIX = ['Input meal: ', 'Output comment: ']
            self.TEST_IN_NAME = 'data/test/healthy_comments_test.txt'

        if self.args.motivate_only:
            self.MODEL_DIR_BASE += '-motivate'
        elif self.args.advice_only:
            self.MODEL_DIR_BASE += '-advice'

        if self.args.match_encoder_decoder_input:
            self.MODEL_DIR_BASE += '-match-decoder'
            
        if self.args.attention:
            self.MODEL_DIR_BASE += '-attention'
            self.args.softmaxSamples = 512
            
        if self.args.food_context:
            self.MODEL_DIR_BASE += '-context'
            self.args.softmaxSamples = 512

        if self.args.first_step:
            self.MODEL_DIR_BASE += '-firstStep'

        if self.args.augment:
            self.MODEL_DIR_BASE += '-augment'

        if self.args.numLayers == 2:
            self.MODEL_DIR_BASE += '-deep'

        if self.args.finetune:
            self.MODEL_DIR_BASE += '-finetune'

        if self.args.MMI:
            self.args.beam_size = 200

        #if self.args.beam_search:
        #    self.MODEL_DIR_BASE += '-beam'

        '''
        # create ranker model
        if self.args.test and self.args.food_context:
            import sys
            sys.path.append('/usr/users/korpusik/LanaServer/Server/Model')
            from ranker import Ranker
            self.args.model = Ranker()
        '''

        if not self.args.rootDir:
            self.args.rootDir = os.getcwd()  # Use the current working directory

        #tf.logging.set_verbosity(tf.logging.INFO) # DEBUG, INFO, WARN (default), ERROR, or FATAL

        self.loadModelParams()  # Update the self.modelDir and self.globStep, for now, not used when loading Model (but need to be called before _getSummaryName)

        self.textData = TextData(self.args)
        # TODO: Add a mode where we can force the input of the decoder // Try to visualize the predictions for
        # each word of the vocabulary / decoder input
        # TODO: For now, the model are trained for a specific dataset (because of the maxLength which define the
        # vocabulary). Add a compatibility mode which allow to launch a model trained on a different vocabulary (
        # remap the word2id/id2word variables).
        if self.args.createDataset:
            print('Dataset created! Thanks for using this program')
            return  # No need to go further

        if self.args.MMI:
            # create bigram language model for MMI scoring of decoder output
            self.probDist = nltk.ConditionalProbDist(nltk.ConditionalFreqDist(nltk.bigrams(self.textData.responseWords)), nltk.MLEProbDist)

        with tf.device(self.getDevice()):
            self.model = Model(self.args, self.textData)

        # Saver/summaries
        self.writer = tf.summary.FileWriter(self._getSummaryName())
        self.saver = tf.train.Saver(max_to_keep=200, write_version=tf.train.SaverDef.V1)  # Arbitrary limit ?

        # TODO: Fixed seed (WARNING: If dataset shuffling, make sure to do that after saving the
        # dataset, otherwise, all which cames after the shuffling won't be replicable when
        # reloading the dataset). How to restore the seed after loading ??
        # Also fix seed for random.shuffle (does it works globally for all files ?)

        # Running session

        self.sess = tf.Session()  # TODO: Replace all sess by self.sess (not necessary a good idea) ?

        print('Initialize variables...')
        self.sess.run(tf.global_variables_initializer())

        # Reload the model eventually (if it exist.), on testing mode, the models are not loaded here (but in predictTestset)
        if self.args.test != Chatbot.TestMode.ALL:
            self.managePreviousModel(self.sess)

        if self.args.test:
            if self.args.test == Chatbot.TestMode.INTERACTIVE:
                self.mainTestInteractive(self.sess)
            elif self.args.test == Chatbot.TestMode.ALL:
                print('Start predicting...')
                self.predictTestset(self.sess)
                print('All predictions done')
            elif self.args.test == Chatbot.TestMode.DAEMON:
                print('Daemon mode, running in background...')
            else:
                raise RuntimeError('Unknown test mode: {}'.format(self.args.test))  # Should never happen
        else:
            self.mainTrain(self.sess)

        if self.args.test != Chatbot.TestMode.DAEMON:
            self.sess.close()
            print("The End! Thanks for using this program")

    def mainTrain(self, sess):
        """ Training loop
        Args:
            sess: The current running session
        """

        # Specific training dependent loading

        self.textData.makeLighter(self.args.ratioDataset)  # Limit the number of training samples

        mergedSummaries = tf.summary.merge_all()  # Define the summary operator (Warning: Won't appear on the tensorboard graph)
        if self.globStep == 0:  # Not restoring from previous run
            self.writer.add_graph(sess.graph)  # First time only

        # If restoring a model, restore the progression bar ? and current batch ?

        print('Start training (press Ctrl+C to save and exit)...')

        try:  # If the user exit while training, we still try to save the model
            for e in range(self.args.numEpochs):

                print()
                print("----- Epoch {}/{} ; (lr={}) -----".format(e+1, self.args.numEpochs, self.args.learningRate))

                batches = self.textData.getBatches()

                # TODO: Also update learning parameters eventually

                tic = datetime.datetime.now()
                for nextBatch in tqdm(batches, desc="Training"):
                    # Training pass
                    ops, feedDict = self.model.step(nextBatch)
                    assert len(ops) == 2  # training, loss
                    _, loss, summary = sess.run(ops + (mergedSummaries,), feedDict)
                    self.writer.add_summary(summary, self.globStep)
                    self.globStep += 1

                    # Checkpoint
                    if self.globStep % self.args.saveEvery == 0:
                        self._saveSession(sess)

                toc = datetime.datetime.now()

                print("Epoch finished in {}".format(toc-tic))  # Warning: Will overflow if an epoch takes more than 24 hours, and the output isn't really nicer
        except (KeyboardInterrupt, SystemExit):  # If the user press Ctrl+C while testing progress
            print('Interruption detected, exiting the program...')

        self._saveSession(sess)  # Ultimate saving before complete exit

    def predictTestset(self, sess):
        """ Try predicting the sentences from the samples.txt file.
        The sentences are saved on the modelDir under the same name
        Args:
            sess: The current running session
        """

        modelList = self._getModelList()
        if not modelList:
            print('Warning: No model found in \'{}\'. Please train a model before trying to predict'.format(self.modelDir))
            return

        if self.args.corpus == 'healthy-comments':
            lines = []
            responses = None
            responses_motivate = []
            responses_advice = []
            corpusDir = '/usr/users/korpusik/nutrition/Talia_data/'
            files = ['salad1.csv', 'salad2.csv', 'salad3.csv', 'dinner1.csv', 'dinner2.csv', 'dinner3.csv', 'pasta1.csv', 'pasta2.csv', 'pasta3.csv', 'pasta4.csv']
            count = 0
            for filen in files:
                csvfile = open(corpusDir + filen)
                reader = csv.DictReader(csvfile)
            
                for row in reader:
                    count += 1
                    # use every 10th line for testing
                    if count % 10 != 0:
                        continue
                    #print(row['Input.meal_response'])
                    lines.append(row['Input.meal_response'])
                    responses_motivate.append(row['Answer.description1'])
                    responses_advice.append(row['Answer.description2'])
            assert len(lines) == len(responses_motivate) == len(responses_advice)
        else:
            # Loading the file to predict
            with open(os.path.join(self.args.rootDir, self.TEST_IN_NAME), 'r') as f:
                lines = f.readlines()
                responses = None

        # Predicting for each model present in modelDir
        meal_response_map = {} # maps meals to list of candidate responses
        for modelName in sorted(modelList):  # TODO: Natural sorting
            print('Restoring previous model from {}'.format(modelName))
            self.saver.restore(sess, modelName)
            print('Testing...')

            saveName = modelName[:-len(self.MODEL_EXT)] + '_predictions_0.1_full_data'  # We remove the model extension and add the prediction suffix
            if self.args.MMI:
                saveName += '_MMI_'+str(self.args.lambda_wt)+'_'+str(self.args.gamma_wt)
            saveName += '.txt'
            if self.args.corpus == 'healthy-comments':
                reference_f1 = open(modelName[:-len(self.MODEL_EXT)] + '_reference_motivate.txt', 'w')
                reference_f2 = open(modelName[:-len(self.MODEL_EXT)] + '_reference_advice.txt', 'w')
                meal_f = open('test_meals.txt', 'w')
            else:
                reference_f = open(modelName[:-len(self.MODEL_EXT)] + self.REFERENCES_SUFFIX, 'w')
            with open(saveName, 'w') as f:
                nbIgnored = 0
                for i, line in enumerate(tqdm(lines, desc='Sentences')):
                    question = line[:-1]  # Remove the endl character
                    if responses:
                        response = responses[i]
                        reference_f.write(response+'\n')
                    elif self.args.corpus == 'healthy-comments':
                        reference_f1.write(responses_motivate[i]+'\n')
                        reference_f2.write(responses_advice[i]+'\n')
                        meal_f.write(question+'\n')

                    answer, predict_responses = self.singlePredict(question)
                    if not answer:
                        nbIgnored += 1
                        continue  # Back to the beginning, try again
                    
                    output = self.textData.sequence2str(answer, clean=True)
                    predict_responses = [self.textData.sequence2str(reply, clean=True) for reply in predict_responses]
                    meal_response_map[question] = predict_responses
                    predString = '{x[0]}{0}\n{x[1]}{1}\n\n'.format(question, output, x=self.SENTENCES_PREFIX)
                    if self.args.verbose:
                        tqdm.write(predString)
                    f.write(output+'\n')
                    
                print('Prediction finished, {}/{} sentences ignored (too long)'.format(nbIgnored, len(lines)))
            if self.args.corpus == 'healthy-comments':
                reference_f1.close()
                reference_f2.close()
                meal_f.close()
            else:
                reference_f.close()

        with open(modelName[:-len(self.MODEL_EXT)] + 'predict_candidates.json', 'w') as fp:
            json.dump(meal_response_map, fp)

        print('Output: ', modelName[:-len(self.MODEL_EXT)] + self.TEST_OUT_SUFFIX)
        print('Refs: ', modelName[:-len(self.MODEL_EXT)] + self.REFERENCES_SUFFIX)

    def mainTestInteractive(self, sess):
        """ Try predicting the sentences that the user will enter in the console
        Args:
            sess: The current running session
        """
        # TODO: If verbose mode, also show similar sentences from the training set with the same words (include in mainTest also)
        # TODO: Also show the top 10 most likely predictions for each predicted output (when verbose mode)
        # TODO: Log the questions asked for latter re-use (merge with test/samples.txt)

        print('Testing: Launch interactive mode:')
        print('')
        print('Welcome to the interactive mode, here you can ask to Deep Q&A the sentence you want. Don\'t have high '
              'expectation. Type \'exit\' or just press ENTER to quit the program. Have fun.')

        while True:
            question = input(self.SENTENCES_PREFIX[0])
            if question == '' or question == 'exit':
                break

            questionSeq = []  # Will be contain the question as seen by the encoder
            answer, candidates = self.singlePredict(question, questionSeq)
            if not answer:
                print('Warning: sentence too long, sorry. Maybe try a simpler sentence.')
                continue  # Back to the beginning, try again

            print('{}{}'.format(self.SENTENCES_PREFIX[1], self.textData.sequence2str(answer, clean=True)))

            if self.args.verbose:
                print(self.textData.batchSeq2str(questionSeq, clean=True, reverse=True))
                print(self.textData.sequence2str(answer))

            print()

    def singlePredict(self, question, questionSeq=None):
        """ Predict the sentence
        Args:
            question (str): the raw input sentence
            questionSeq (List<int>): output argument. If given will contain the input batch sequence
        Return:
            list <int>: the word ids corresponding to the answer
        """
        # Create the input batch
        batch = self.textData.sentence2enco(question)
        if not batch:
            return None
        if questionSeq is not None:  # If the caller want to have the real input
            questionSeq.extend(batch.encoderSeqs)

        # Run the model
        ops, feedDict = self.model.step(batch, self.args.match_encoder_decoder_input)
        output = self.sess.run(ops[0], feedDict)  # TODO: Summarize the output too (histogram, ...)
        candidates = []
        if self.args.beam_search:
            # print all candidates in beam
            probs, path, symbol, output = output[-1], output[-3], output[-2], output[:-3]
            #print('probs', probs)
            #print('path', path)
            #print('symbol', symbol)
            
            paths = []
            log_probs = [] # total log prob of each candidate path
            num_steps = len(path)
            lastTokenIndex = [num_steps]*self.args.beam_size
            for kk in range(self.args.beam_size):
                paths.append([])
                log_probs.append(0.0)
                for i in range(num_steps-1):
                    if symbol[i][kk] == self.textData.eosToken:
                        lastTokenIndex[kk] = i
                        break
            curr = list(range(self.args.beam_size))
            for i in range(num_steps-1, -1, -1):
                for kk in range(self.args.beam_size):
                    if i > lastTokenIndex[kk]:
                        continue
                    paths[kk].append(symbol[i][curr[kk]])
                    log_probs[kk] = log_probs[kk] + probs[i][curr[kk]]
                    curr[kk] = path[i][curr[kk]]

            #print ("Replies ---------------------->")
            reply_score_map = {}
            best_score = None
            for kk in range(self.args.beam_size):
                foutputs = [int(logit) for logit in paths[kk][::-1]]
                candidates.append(foutputs)
                #print(foutputs)
                reply = self.textData.sequence2str(foutputs, clean=True)
                if reply in reply_score_map:
                    continue
                if self.args.MMI:
                    length_term = self.args.gamma_wt * len(paths[kk])
                    log_LM_penalty = 0.0
                    prevWord = "<start>"
                    for wordID in foutputs[:self.args.gamma_wt]:
                        currWord = self.textData.id2word[wordID]
                        bigramP = self.probDist[prevWord].prob(currWord)
                        # TODO: try Kneser-Ney smoothing
                        if bigramP > 0:
                            log_LM_penalty += math.log(bigramP)
                        prevWord = currWord
                    # TODO: try with product of probs instead of sum of logs
                    LM_term = self.args.lambda_wt * log_LM_penalty
                    score = log_probs[kk] - LM_term + length_term
                    reply_score_map[reply] = score
                    #print(score, log_probs[kk], LM_term, length_term, reply)
                else:
                    print(reply)
                    score = log_probs[kk]
                    reply_score_map[reply] = log_probs[kk]
                if kk == 0:
                    answer = foutputs
                    best_score = score
                elif score > best_score:
                    answer = foutputs
                    best_score = score
            # rerank replies based on MMI scores
            if self.args.MMI:
                sorted_replies = sorted(reply_score_map.items(), key=operator.itemgetter(1), reverse=True)
                for i, (reply, score) in enumerate(sorted_replies):
                    if self.args.test == 'interactive':
                        print(i, score, reply)
        else:
            answer = self.textData.deco2sentence(output)
            candidates.append(answer)

        return answer, candidates

    def daemonPredict(self, sentence):
        """ Return the answer to a given sentence (same as singlePredict() but with additional cleaning)
        Args:
            sentence (str): the raw input sentence
        Return:
            str: the human readable sentence
        """
        return self.textData.sequence2str(
            self.singlePredict(sentence),
            clean=True
        )

    def daemonClose(self):
        """ A utility function to close the daemon when finish
        """
        print('Exiting the daemon mode...')
        self.sess.close()
        print('Daemon closed.')

    def managePreviousModel(self, sess):
        """ Restore or reset the model, depending of the parameters
        If the destination directory already contains some file, it will handle the conflict as following:
         * If --reset is set, all present files will be removed (warning: no confirmation is asked) and the training
         restart from scratch (globStep & cie reinitialized)
         * Otherwise, it will depend of the directory content. If the directory contains:
           * No model files (only summary logs): works as a reset (restart from scratch)
           * Other model files, but modelName not found (surely keepAll option changed): raise error, the user should
           decide by himself what to do
           * The right model file (eventually some other): no problem, simply resume the training
        In any case, the directory will exist as it has been created by the summary writer
        Args:
            sess: The current running session
        """

        print('WARNING: ', end='')

        modelName = self._getModelName()

        if os.listdir(self.modelDir):
            if self.args.reset:
                print('Reset: Destroying previous model at {}'.format(self.modelDir))
            # Analysing directory content
            elif os.path.exists(modelName):  # Restore the model
                print('Restoring previous model from {}'.format(modelName))
                self.saver.restore(sess, modelName)  # Will crash when --reset is not activated and the model has not been saved yet
                print('Model restored.')
            elif self._getModelList():
                print('Conflict with previous models.')
                raise RuntimeError('Some models are already present in \'{}\'. You should check them first (or re-try with the keepAll flag)'.format(self.modelDir))
            else:  # No other model to conflict with (probably summary files)
                print('No previous model found, but some files found at {}. Cleaning...'.format(self.modelDir))  # Warning: No confirmation asked
                self.args.reset = True

            if self.args.reset:
                fileList = [os.path.join(self.modelDir, f) for f in os.listdir(self.modelDir)]
                for f in fileList:
                    print('Removing {}'.format(f))
                    os.remove(f)

        else:
            print('No previous model found, starting from clean directory: {}'.format(self.modelDir))

    def _saveSession(self, sess):
        """ Save the model parameters and the variables
        Args:
            sess: the current session
        """
        tqdm.write('Checkpoint reached: saving model (don\'t stop the run)...')
        self.saveModelParams()
        self.saver.save(sess, self._getModelName())  # TODO: Put a limit size (ex: 3GB for the modelDir)
        tqdm.write('Model saved.')

    def _getModelList(self):
        """ Return the list of the model files inside the model directory
        """
        return [os.path.join(self.modelDir, f) for f in os.listdir(self.modelDir) if f.endswith(self.MODEL_EXT)]

    def loadModelParams(self):
        """ Load the some values associated with the current model, like the current globStep value
        For now, this function does not need to be called before loading the model (no parameters restored). However,
        the modelDir name will be initialized here so it is required to call this function before managePreviousModel(),
        _getModelName() or _getSummaryName()
        Warning: if you modify this function, make sure the changes mirror saveModelParams, also check if the parameters
        should be reset in managePreviousModel
        """
        # Compute the current model path
        self.modelDir = os.path.join(self.args.rootDir, self.MODEL_DIR_BASE)
        if self.args.modelTag:
            self.modelDir += '-' + self.args.modelTag

        # If there is a previous model, restore some parameters
        configName = os.path.join(self.modelDir, self.CONFIG_FILENAME)
        if not self.args.reset and not self.args.createDataset and os.path.exists(configName):
            # Loading
            config = configparser.ConfigParser()
            config.read(configName)

            # Check the version
            currentVersion = config['General'].get('version')
            if currentVersion != self.CONFIG_VERSION:
                raise UserWarning('Present configuration version {0} does not match {1}. You can try manual changes on \'{2}\''.format(currentVersion, self.CONFIG_VERSION, configName))

            # Restoring the the parameters
            self.globStep = config['General'].getint('globStep')
            self.args.maxLength = config['General'].getint('maxLength')  # We need to restore the model length because of the textData associated and the vocabulary size (TODO: Compatibility mode between different maxLength)
            self.args.watsonMode = config['General'].getboolean('watsonMode')
            #self.args.datasetTag = config['General'].get('datasetTag')

            self.args.hiddenSize = config['Network'].getint('hiddenSize')
            self.args.numLayers = config['Network'].getint('numLayers')
            self.args.embeddingSize = config['Network'].getint('embeddingSize')
            self.args.softmaxSamples = config['Network'].getint('softmaxSamples')

            # No restoring for training params, batch size or other non model dependent parameters

            # Show the restored params
            print()
            print('Warning: Restoring parameters:')
            print('globStep: {}'.format(self.globStep))
            print('maxLength: {}'.format(self.args.maxLength))
            print('watsonMode: {}'.format(self.args.watsonMode))
            print('hiddenSize: {}'.format(self.args.hiddenSize))
            print('numLayers: {}'.format(self.args.numLayers))
            print('embeddingSize: {}'.format(self.args.embeddingSize))
            print('softmaxSamples: {}'.format(self.args.softmaxSamples))
            print()

        # For now, not arbitrary  independent maxLength between encoder and decoder
        self.args.maxLengthEnco = self.args.maxLength
        self.args.maxLengthDeco = self.args.maxLength + 2

        if self.args.watsonMode:
            self.SENTENCES_PREFIX.reverse()


    def saveModelParams(self):
        """ Save the params of the model, like the current globStep value
        Warning: if you modify this function, make sure the changes mirror loadModelParams
        """
        config = configparser.ConfigParser()
        config['General'] = {}
        config['General']['version']  = self.CONFIG_VERSION
        config['General']['globStep']  = str(self.globStep)
        config['General']['maxLength'] = str(self.args.maxLength)
        config['General']['watsonMode'] = str(self.args.watsonMode)

        config['Network'] = {}
        config['Network']['hiddenSize'] = str(self.args.hiddenSize)
        config['Network']['numLayers'] = str(self.args.numLayers)
        config['Network']['embeddingSize'] = str(self.args.embeddingSize)
        config['Network']['softmaxSamples'] = str(self.args.softmaxSamples)

        # Keep track of the learning params (but without restoring them)
        config['Training (won\'t be restored)'] = {}
        config['Training (won\'t be restored)']['learningRate'] = str(self.args.learningRate)
        config['Training (won\'t be restored)']['batchSize'] = str(self.args.batchSize)

        with open(os.path.join(self.modelDir, self.CONFIG_FILENAME), 'w') as configFile:
            config.write(configFile)

    def _getSummaryName(self):
        """ Parse the argument to decide were to save the summary, at the same place that the model
        The folder could already contain logs if we restore the training, those will be merged
        Return:
            str: The path and name of the summary
        """
        return self.modelDir

    def _getModelName(self):
        """ Parse the argument to decide were to save/load the model
        This function is called at each checkpoint and the first time the model is load. If keepAll option is set, the
        globStep value will be included in the name.
        Return:
            str: The path and name were the model need to be saved
        """
        modelName = os.path.join(self.modelDir, self.MODEL_NAME_BASE)
        if self.args.keepAll:  # We do not erase the previously saved model by including the current step on the name
            modelName += '-' + str(self.globStep)
        return modelName + self.MODEL_EXT

    def getDevice(self):
        """ Parse the argument to decide on which device run the model
        Return:
            str: The name of the device on which run the program
        """
        if self.args.device == 'cpu':
            return '/cpu:0'
        elif self.args.device == 'gpu':
            return '/gpu:0'
        elif self.args.device is None:  # No specified device (default)
            return None
        else:
            print('Warning: Error in the device name: {}, use the default device'.format(self.args.device))
            return None
