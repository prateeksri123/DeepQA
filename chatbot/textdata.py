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
Loads the dialogue corpus, builds the vocabulary
"""

import numpy as np
import nltk  # For tokenize
from tqdm import tqdm  # Progress bar
import pickle  # Saving the data
import math  # For float comparison
import os  # Checking file existance
import random
import numpy as np
import urllib
import json

from chatbot.cornelldata import CornellData
from chatbot.mealdata import MealData
from chatbot.healthydata import HealthyData


class Batch:
    """Struct containing batches info
    """
    def __init__(self):
        self.encoderSeqs = []
        self.decoderSeqs = []
        self.contextSeqs = []
        self.targetSeqs = []
        self.weights = []


class TextData:
    """Dataset class
    Warning: No vocabulary limit
    """

    def __init__(self, args):
        """Load all conversations
        Args:
            args: parameters of the model
        """
        # Model parameters
        self.args = args

        # Path variables
        if self.args.corpus == 'cornell':
            self.corpusDir = os.path.join(self.args.rootDir, 'data/cornell/')
        if self.args.all_data:
            self.samplesDir = os.path.join(self.args.rootDir, 'data/samples_allData/')
        else:
            self.samplesDir = os.path.join(self.args.rootDir, 'data/samples/')
        if self.args.corpus == 'nutrition':
            self.corpusDir = '/usr/users/zcollins/Data_Files/allfood/'
            if self.args.encode_food_descrips:
                self.samplesDir += 'food-descrip'
            elif self.args.encode_single_food_descrip:
                self.samplesDir += 'single-food-descrip'
            elif self.args.encode_food_ids:
                self.samplesDir += 'food-id'
            else:
                self.samplesDir += 'meal'

            if self.args.match_encoder_decoder_input:
                self.samplesDir += '-match-decoder'

            self.samplesDir += '/'
        elif self.args.corpus == 'healthy-comments':
            self.corpusDir = '/usr/users/korpusik/nutrition/Talia_data/'
            self.samplesDir += 'healthy-comments'

            if self.args.motivate_only:
                self.samplesDir += '-motivate'
            elif self.args.advice_only:
                self.samplesDir += '-advice'

            if self.args.healthy_flag:
                self.samplesDir += '-flag'
            elif self.args.encode_food_ids:
                self.samplesDir += '-foodID'
            elif self.args.food_context:
                self.samplesDir += '-context'

            if self.args.augment:
                self.samplesDir += '-augment'
            
        self.samplesName = self._constructName()
        print(self.samplesDir, self.samplesName)

        self.padToken = -1  # Padding
        self.goToken = -1  # Start of sequence
        self.eosToken = -1  # End of sequence
        self.unknownToken = -1  # Word dropped from vocabulary

        self.trainingSamples = []  # 2d array containing each question and his answer [[input,target]]

        self.word2id = {}
        self.id2word = {}  # For a rapid conversion

        self.loadCorpus(self.samplesDir)

        # Plot some stats:
        print('Loaded: {} words, {} QA'.format(len(self.word2id), len(self.trainingSamples)))

        if self.args.playDataset:
            self.playDataset()

    def _constructName(self):
        """Return the name of the dataset that the program should use with the current parameters.
        Computer from the base name, the given tag (self.args.datasetTag) and the sentence length
        """
        baseName = 'dataset'
        if self.args.datasetTag:
            baseName += '-' + self.args.datasetTag
        return baseName + '-' + str(self.args.maxLength) + '.pkl'

    def makeLighter(self, ratioDataset):
        """Only keep a small fraction of the dataset, given by the ratio
        """
        #if not math.isclose(ratioDataset, 1.0):
        #    self.shuffle()  # Really ?
        #    print('WARNING: Ratio feature not implemented !!!')
        pass

    def shuffle(self):
        """Shuffle the training samples
        """
        print("Shuffling the dataset...")
        random.shuffle(self.trainingSamples)

    def _createBatch(self, samples):
        """Create a single batch from the list of sample. The batch size is automatically defined by the number of
        samples given.
        The inputs should already be inverted. The target should already have <go> and <eos>
        Warning: This function should not make direct calls to args.batchSize !!!
        Args:
            samples (list<Obj>): a list of samples, each sample being on the form [input, target]
        Return:
            Batch: a batch object en
        """

        batch = Batch()
        batchSize = len(samples)

        # Create the batch tensor
        for i in range(batchSize):
            # Unpack the sample
            sample = samples[i]
            if not self.args.test and self.args.watsonMode:  # Watson mode: invert question and answer
                sample = list(reversed(sample))
            batch.encoderSeqs.append(list(reversed(sample[0])))  # Reverse inputs (and not outputs), little trick as defined on the original seq2seq paper
            target_seq_with_go = [self.goToken] + sample[1] + [self.eosToken]
            if self.args.match_encoder_decoder_input:
                # use encoder input as decoder input
                batch.decoderSeqs.append([self.goToken] + sample[0] + [self.eosToken])
            else:
                batch.decoderSeqs.append(target_seq_with_go)  # Add the <go> and <eos> tokens
            if self.args.food_context:
                if self.args.first_step:
                    batch.contextSeqs.append([sample[2]]+[np.zeros(64,)]*(self.args.maxLengthDeco - 1)) # add food embedding context
                else:
                    batch.contextSeqs.append([sample[2]]*self.args.maxLengthDeco) # add food embedding context
            batch.targetSeqs.append(target_seq_with_go[1:])  # target seq, but shifted to the left (ignore the <go>)

            # Long sentences should have been filtered during the dataset creation
            assert len(batch.encoderSeqs[i]) <= self.args.maxLengthEnco
            assert len(batch.decoderSeqs[i]) <= self.args.maxLengthDeco

            # Add padding & define weight
            batch.encoderSeqs[i]   = [self.padToken] * (self.args.maxLengthEnco  - len(batch.encoderSeqs[i])) + batch.encoderSeqs[i]  # Left padding for the input
            batch.weights.append([1.0] * len(batch.targetSeqs[i]) + [0.0] * (self.args.maxLengthDeco - len(batch.targetSeqs[i])))
            batch.decoderSeqs[i] = batch.decoderSeqs[i] + [self.padToken] * (self.args.maxLengthDeco - len(batch.decoderSeqs[i]))
            batch.targetSeqs[i]  = batch.targetSeqs[i]  + [self.padToken] * (self.args.maxLengthDeco - len(batch.targetSeqs[i]))

        # Simple hack to reshape the batch
        encoderSeqsT = []  # Corrected orientation
        for i in range(self.args.maxLengthEnco):
            encoderSeqT = []
            for j in range(batchSize):
                encoderSeqT.append(batch.encoderSeqs[j][i])
            encoderSeqsT.append(encoderSeqT)
        batch.encoderSeqs = encoderSeqsT

        decoderSeqsT = []
        targetSeqsT = []
        weightsT = []
        contextSeqsT = []
        for i in range(self.args.maxLengthDeco):
            decoderSeqT = []
            targetSeqT = []
            weightT = []
            contextSeqT = []
            for j in range(batchSize):
                decoderSeqT.append(batch.decoderSeqs[j][i])
                targetSeqT.append(batch.targetSeqs[j][i])
                weightT.append(batch.weights[j][i])
                if self.args.food_context:
                    contextSeqT.append(batch.contextSeqs[j][i])
                else:
                    contextSeqT.append(np.zeros(64,))
            decoderSeqsT.append(decoderSeqT)
            targetSeqsT.append(targetSeqT)
            weightsT.append(weightT)
            contextSeqsT.append(contextSeqT)
        batch.decoderSeqs = decoderSeqsT
        batch.targetSeqs = targetSeqsT
        batch.weights = weightsT
        batch.contextSeqs = contextSeqsT

        # # Debug
        # self.printBatch(batch)  # Input inverted, padding should be correct
        # print(self.sequence2str(samples[0][0]))
        # print(self.sequence2str(samples[0][1]))  # Check we did not modified the original sample

        return batch

    def getBatches(self):
        """Prepare the batches for the current epoch
        Return:
            list<Batch>: Get a list of the batches for the next epoch
        """
        self.shuffle()

        batches = []

        def genNextSamples():
            """ Generator over the mini-batch training samples
            """
            for i in range(0, self.getSampleSize(), self.args.batchSize):
                yield self.trainingSamples[i:min(i + self.args.batchSize, self.getSampleSize())]

        for samples in genNextSamples():
            batch = self._createBatch(samples)
            batches.append(batch)
        return batches

    def getSampleSize(self):
        """Return the size of the dataset
        Return:
            int: Number of training samples
        """
        return len(self.trainingSamples)

    def getVocabularySize(self):
        """Return the number of words present in the dataset
        Return:
            int: Number of word on the loader corpus
        """
        return len(self.word2id)

    def loadCorpus(self, dirName):
        """Load/create the conversations data
        Args:
            dirName (str): The directory where to load/save the model
        """
        datasetExist = False
        if os.path.exists(os.path.join(dirName, self.samplesName)):
            datasetExist = True

        if not datasetExist:  # First time we load the database: creating all files
            print('Training samples not found. Creating dataset...')
            # Corpus creation
            if self.args.corpus == 'cornell':
                cornellData = CornellData(self.corpusDir)
                self.createCorpus(cornellData.getConversations())
            elif self.args.corpus == 'nutrition':
                mealData = MealData(self.corpusDir)
        
                if self.args.encode_food_descrips:
                    self.createCorpus(zip(mealData.getFoodDescrips(), mealData.getMeals()))
                elif self.args.encode_single_food_descrip:
                    self.createCorpus(zip(mealData.getSingleFoodDescrips(), mealData.getAlignments()))
                elif self.args.encode_food_ids:
                    self.createCorpus(zip(mealData.getFoodIDs(), mealData.getMeals()))
                else:
                    self.createCorpus(mealData.getMeals())
            elif self.args.corpus == 'healthy-comments':
                self.healthyData = HealthyData(self.corpusDir, self.args.usda_vecs, self.args.healthy_flag, self.args.augment, self.args.motivate_only, self.args.advice_only, self.args.all_data)
                if self.args.encode_food_ids:
                    self.createCorpus(zip(self.healthyData.getFoodIDs(), self.healthyData.getResponses()))
                else:
                    self.createCorpus(zip(self.healthyData.getMeals(), self.healthyData.getResponses(), self.healthyData.getFoodEmb()))

            # Saving
            print('Saving dataset...')
            self.saveDataset(dirName)  # Saving tf samples
        else:
            print('Loading dataset from {}...'.format(dirName))
            self.loadDataset(dirName)
            if self.args.finetune and not self.args.test:
                self.trainingSamples = []
                mealData = MealData('/usr/users/zcollins/Data_Files/allfood/')
                self.createCorpus(mealData.getMeals())

        assert self.padToken == 0

    def saveDataset(self, dirName):
        """Save samples to file
        Args:
            dirName (str): The directory where to load/save the model
        """

        with open(os.path.join(dirName, self.samplesName), 'wb') as handle:
            data = {  # Warning: If adding something here, also modifying loadDataset
                "word2id": self.word2id,
                "id2word": self.id2word,
                "trainingSamples": self.trainingSamples,
                "responseWords": self.healthyData.getWords()
                }
            pickle.dump(data, handle, -1)  # Using the highest protocol available

    def loadDataset(self, dirName):
        """Load samples from file
        Args:
            dirName (str): The directory where to load the model
        """
        with open(os.path.join(dirName, self.samplesName), 'rb') as handle:
            data = pickle.load(handle)  # Warning: If adding something here, also modifying saveDataset
            self.word2id = data["word2id"]
            self.id2word = data["id2word"]
            self.trainingSamples = data["trainingSamples"]
            self.responseWords = data["responseWords"]

            self.padToken = self.word2id["<pad>"]
            self.goToken = self.word2id["<go>"]
            self.eosToken = self.word2id["<eos>"]
            self.unknownToken = self.word2id["<unknown>"]  # Restore special words

    def createCorpus(self, conversations):
        """Extract all data from the given vocabulary
        """
        # Add standard tokens
        self.padToken = self.getWordId("<pad>")  # Padding (Warning: first things to add > id=0 !!)
        self.goToken = self.getWordId("<go>")  # Start of sequence
        self.eosToken = self.getWordId("<eos>")  # End of sequence
        self.unknownToken = self.getWordId("<unknown>")  # Word dropped from vocabulary

        # Preprocessing data

        
        for conversation in tqdm(conversations, desc="Extract conversations"):
            if self.args.corpus == 'cornell':
                self.extractConversation(conversation)
            elif self.args.encode_food_descrips or self.args.encode_food_ids:
                self.extractFoods(conversation[0], conversation[1])
            elif self.args.corpus == 'healthy-comments' and not self.args.finetune:
                self.extractHealthyComments(conversation[0], conversation[1], conversation[2])
            elif self.args.encode_single_food_descrip:
                self.extractFoods([conversation[0]], conversation[1])
            else:
                # encode and decode meals
                self.extractMeal(conversation)

        # The dataset will be saved in the same order it has been extracted

    def extractConversation(self, conversation):
        """Extract the sample lines from the conversations
        Args:
            conversation (Obj): a conversation object containing the lines to extract
        """

        # Iterate over all the lines of the conversation
        for i in range(len(conversation["lines"]) - 1):  # We ignore the last line (no answer for it)
            inputLine  = conversation["lines"][i]
            targetLine = conversation["lines"][i+1]

            inputWords  = self.extractText(inputLine["text"])
            targetWords = self.extractText(targetLine["text"], True)

            if inputWords and targetWords:  # Filter wrong samples (if one of the list is empty)
                self.trainingSamples.append([inputWords, targetWords])

    def extractMeal(self, meal):
        """Extract the sample meal descriptions
        Args:
            meal (str): the meal description text
        """
        inputWords  = self.extractText(meal)
        targetWords = self.extractText(meal, True)

        if inputWords and targetWords:  # Filter wrong samples (if one of the list is empty)
                self.trainingSamples.append([inputWords, targetWords])

    def extractHealthyComments(self, meal, response, foods):
        """Extract the sample meal descriptions and healthy/unhealthy comments
        Args:
            meal (str): the meal description text
            response (str): the healthy/unhealthy commentary
        """
        inputWords  = self.extractText(meal)
        targetWords = self.extractText(response, True)

        if inputWords and targetWords:  # Filter wrong samples (if one of the list is empty)
                self.trainingSamples.append([inputWords, targetWords, foods])

    def extractFoods(self, foods, meal):
        """Extract the sample's matching food descriptions
        Args:
            foods (list): the matching food descriptions
        """
        inputWords  = []
        if self.args.encode_food_descrips or self.args.encode_single_food_descrip:
            for food_descrip in foods:
                inputWords.extend(self.extractText(food_descrip))
        elif self.args.encode_food_ids:
            for food_id in foods:
                inputWords.append(self.getWordId(food_id))
        targetWords = self.extractText(meal, True)

        if inputWords and targetWords:  # Filter wrong samples (if one of the list is empty)
                self.trainingSamples.append([inputWords, targetWords])

    def extractText(self, line, isTarget=False):
        """Extract the words from a sample lines
        Args:
            line (str): a line containing the text to extract
            isTarget (bool): Define the question on the answer
        Return:
            list<int>: the list of the word ids of the sentence
        """
        words = []

        # Extract sentences
        sentencesToken = nltk.sent_tokenize(line)

        # We add sentence by sentence until we reach the maximum length
        for i in range(len(sentencesToken)):
            # If question: we only keep the last sentences
            # If answer: we only keep the first sentences
            if not isTarget:
                i = len(sentencesToken)-1 - i

            tokens = nltk.word_tokenize(sentencesToken[i])

            # If the total length is not too big, we still can add one more sentence
            if len(words) + len(tokens) <= self.args.maxLength:
                tempWords = []
                for token in tokens:
                    if self.args.finetune:
                        tempWords.append(self.getWordId(token, create=False))
                    else:
                        tempWords.append(self.getWordId(token))  # Create the vocabulary and the training sentences

                if isTarget:
                    words = words + tempWords
                else:
                    words = tempWords + words
            else:
                break  # We reach the max length already

        return words

    def getWordId(self, word, create=True):
        """Get the id of the word (and add it to the dictionary if not existing). If the word does not exist and
        create is set to False, the function will return the unknownToken value
        Args:
            word (str): word to add
            create (Bool): if True and the word does not exist already, the world will be added
        Return:
            int: the id of the word created
        """
        # Should we Keep only words with more than one occurrence ?

        word = word.lower()  # Ignore case

        # Get the id if the word already exist
        wordId = self.word2id.get(word, -1)

        # If not, we create a new entry
        if wordId == -1:
            if create:
                wordId = len(self.word2id)
                self.word2id[word] = wordId
                self.id2word[wordId] = word
            else:
                wordId = self.unknownToken

        return wordId

    def printBatch(self, batch):
        """Print a complete batch, useful for debugging
        Args:
            batch (Batch): a batch object
        """
        print('----- Print batch -----')
        for i in range(len(batch.encoderSeqs[0])):  # Batch size
            print('Encoder: {}'.format(self.batchSeq2str(batch.encoderSeqs, seqId=i)))
            print('Decoder: {}'.format(self.batchSeq2str(batch.decoderSeqs, seqId=i)))
            print('Targets: {}'.format(self.batchSeq2str(batch.targetSeqs, seqId=i)))
            print('Weights: {}'.format(' '.join([str(weight) for weight in [batchWeight[i] for batchWeight in batch.weights]])))

    def sequence2str(self, sequence, clean=False, reverse=False):
        """Convert a list of integer into a human readable string
        Args:
            sequence (list<int>): the sentence to print
            clean (Bool): if set, remove the <go>, <pad> and <eos> tokens
            reverse (Bool): for the input, option to restore the standard order
        Return:
            str: the sentence
        """

        if not sequence:
            return ''

        if not clean:
            return ' '.join([self.id2word[idx] for idx in sequence])

        sentence = []
        for wordId in sequence:
            if wordId == self.eosToken:  # End of generated sentence
                break
            elif wordId != self.padToken and wordId != self.goToken:
                sentence.append(self.id2word[wordId])

        if reverse:  # Reverse means input so no <eos> (otherwise pb with previous early stop)
            sentence.reverse()

        return ' '.join(sentence)

    def batchSeq2str(self, batchSeq, seqId=0, **kwargs):
        """Convert a list of integer into a human readable string.
        The difference between the previous function is that on a batch object, the values have been reorganized as
        batch instead of sentence.
        Args:
            batchSeq (list<list<int>>): the sentence(s) to print
            seqId (int): the position of the sequence inside the batch
            kwargs: the formatting options( See sequence2str() )
        Return:
            str: the sentence
        """
        sequence = []
        for i in range(len(batchSeq)):  # Sequence length
            sequence.append(batchSeq[i][seqId])
        return self.sequence2str(sequence, **kwargs)

    def sentence2enco(self, sentence):
        """Encode a sequence and return a batch as an input for the model
        Return:
            Batch: a batch object containing the sentence, or none if something went wrong
        """

        if sentence == '':
            return None

        # First step: Divide the sentence in token
        tokens = nltk.word_tokenize(sentence)
        if len(tokens) > self.args.maxLength:
            return None

        # Second step: Convert the token in word ids
        wordIds = []
        for token in tokens:
            wordIds.append(self.getWordId(token, create=False))  # Create the vocabulary and the training sentences

        # Third step: creating the batch (add padding, reverse)
        sample = []
        # predict foods, then sum food embeddings
        if self.args.food_context:
            #output_map = self.args.model.run_model([sentence])
            #foodIDs = [food_seg['Hits'][0][1:] for food_seg in output_map.values()]
            meal = sentence.replace(" ", "%20")
            foodIDs = json.loads(urllib.request.urlopen("http://128.30.34.150:5000/lana/api/v1.0/query_IDs?raw_text="+meal).read().decode('utf-8'))
            print('foods', foodIDs)
            embeddings = np.sum([self.args.usda_vecs[foodID] for foodID in foodIDs], axis=0)
            batch = self._createBatch([[wordIds, [], embeddings]])
        else:
            batch = self._createBatch([[wordIds, []]])  # Mono batch, no target output

        return batch

    def deco2sentence(self, decoderOutputs):
        """Decode the output of the decoder and return a human friendly sentence
        decoderOutputs (list<np.array>):
        """
        sequence = []

        # Choose the words with the highest prediction score
        for out in decoderOutputs:
            sequence.append(np.argmax(out))  # Adding each predicted word ids

        return sequence  # We return the raw sentence. Let the caller do some cleaning eventually

    def playDataset(self):
        """Print a random dialogue from the dataset
        """
        print('Randomly play samples:')
        for i in range(self.args.playDataset):
            idSample = random.randint(0, len(self.trainingSamples))
            print('Q: {}'.format(self.sequence2str(self.trainingSamples[idSample][0])))
            print('A: {}'.format(self.sequence2str(self.trainingSamples[idSample][1])))
            print()
        pass
