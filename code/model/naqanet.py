import torch
import torch.nn as nn
import numpy as np

from torch.nn.utils.rnn import pad_sequence

from code.modules.encoder.encoder import EncoderBlock
from code.modules.encoder.depthwise_conv import DepthwiseSeparableConv
from code.modules.pointer import Pointer
from code.modules.cq_attention import CQAttention
from code.modules.embeddings import Embedding
from code.modules.utils import set_mask
from code.util import torch_from_json, masked_softmax
from code.args import get_train_args
from code.model.qanet import QANet


class NAQANet(QANet):
    def __init__(self, 
                 device,
                 word_embeddings,
                 char_embeddings,
                 w_emb_size:int = 300,
                 c_emb_size:int = 64,
                 hidden_size:int = 128,
                 c_max_len: int = 800,
                 q_max_len: int = 100,
                 p_dropout: float = 0.1,
                 num_heads : int = 8, 
                 answering_abilities = ['passage_span_extraction', 'counting', 'addition_subtraction'],
                 max_count = 10): # max number the network can count
        """
        :param hidden_size: hidden size of representation vectors
        :param q_max_len: max number of words in a question sentence
        :param c_max_len: max number of words in a context sentence
        :param p_dropout: dropout probability
        """
        super().__init__(
            device, 
            word_embeddings,
            char_embeddings,
            w_emb_size,
            c_emb_size,
            hidden_size,
            c_max_len,
            q_max_len,
            p_dropout,
            num_heads)

        # Implementing numerically augmented output for QANet
        self.answering_abilities = answering_abilities
        self.max_count = max_count

        # pasage and question representations coefficients
        self.passage_weights_layer = nn.Linear(hidden_size, 1)
        self.question_weights_layer = nn.Linear(hidden_size, 1)

        # answer type predictor
        if len(self.answering_abilities) > 1:
            self.answer_ability_predictor = nn.Sequential(
                nn.Linear(2*hidden_size, hidden_size),
                nn.ReLU(), 
                nn.Dropout(p = self.p_dropout),
                nn.Linear(hidden_size, len(self.answering_abilities)),
                nn.ReLU(), 
                nn.Dropout(p = self.p_dropout)
            ) # then, apply a softmax
        

        if 'passage_span_extraction' in self.answering_abilities:
            self.passage_span_extraction_index = self.answering_abilities.index(
                "passage_span_extraction"
            )
            self.passage_span_start_predictor = nn.Sequential(
                nn.Linear(hidden_size * 2, hidden_size),
                nn.ReLU(), 
                nn.Linear(hidden_size, 1),
                nn.ReLU()
            )
            self.passage_span_end_predictor = nn.Sequential(
                nn.Linear(hidden_size * 2, hidden_size),
                nn.ReLU(), 
                nn.Linear(hidden_size, 1),
                nn.ReLU()
            ) # then, apply a softmax

        if 'counting' in self.answering_abilities:
            self.counting_index = self.answering_abilities.index("counting")
            self.count_number_predictor = nn.Sequential(
                nn.Linear(hidden_size, hidden_size),
                nn.ReLU(), 
                nn.Dropout(p = self.p_dropout),
                nn.Linear(hidden_size, self.max_count),
                nn.ReLU()
            ) # then, apply a softmax
        
        if 'addition_subtraction' in self.answering_abilities:
            self.addition_subtraction_index = self.answering_abilities.index(
                "addition_subtraction"
            )
            self.number_sign_predictor = nn.Sequential(
                nn.Linear(hidden_size*3, hidden_size),
                nn.ReLU(),
                nn.Linear(hidden_size, 3),
                nn.ReLU()
            )

    def forward(self, cw_idxs, cc_idxs, qw_idxs, qc_idxs, number_indices):

        _, _ = super().forward(cw_idxs, cc_idxs, qw_idxs, qc_idxs)

        # The first modeling layer is used to calculate the vector representation of passage
        passage_weights = masked_softmax(self.passage_weights_layer(self.passage_aware_rep).squeeze(-1), self.c_mask_c2q, log_softmax = False)
        passage_vector_rep = passage_weights.unsqueeze(1).bmm(self.passage_aware_rep).squeeze(1)
        # The second modeling layer is use to calculate the vector representation of question
        question_weights = masked_softmax(self.question_weights_layer(self.qb).squeeze(-1), self.q_mask_c2q, log_softmax = False)
        question_vector_rep = question_weights.unsqueeze(1).bmm(self.qb).squeeze(1)

        if len(self.answering_abilities) > 1:
            # Shape: (batch_size, number_of_abilities)
            answer_ability_logits = self.answer_ability_predictor(
                torch.cat([passage_vector_rep, question_vector_rep], -1)
            )
            answer_ability_log_probs = torch.nn.functional.log_softmax(answer_ability_logits, -1)
            # Shape: (batch_size,)
            best_answer_ability = torch.argmax(answer_ability_log_probs, 1)

        if "counting" in self.answering_abilities:
            # Shape: (batch_size, self.max_count)
            count_number_logits = self.count_number_predictor(passage_vector_rep)
            count_number_log_probs = torch.nn.functional.log_softmax(count_number_logits, -1) # softmax over possible numbers

            # Info about the best count number prediction
            # Shape: (batch_size,)
            best_count_number = torch.argmax(count_number_log_probs, -1) # return the most probable number value
            best_count_log_prob = torch.gather(
                count_number_log_probs, 1, best_count_number.unsqueeze(-1)
            ).squeeze(-1)
            
            if len(self.answering_abilities) > 1:
                best_count_log_prob += answer_ability_log_probs[:, self.counting_index]

        # Sto buttando il mio tempo
        if "addition_subtraction" in self.answering_abilities:
            # M3
            modeled_passage = self.dropout_layer(
                self.modeling_encoder_block(self.modeled_passage_list[-1], self.c_mask_enc)
            )
            self.modeled_passage_list.append(modeled_passage)
            encoded_passage_for_numbers = torch.cat(
                [self.modeled_passage_list[0], self.modeled_passage_list[3]], dim=-1
            )

            # Reshape number indices from (total number indices, 2) to (batch_size, # numbers in longest passage)
            batch_size = encoded_passage_for_numbers.size(0)
            formatted_num_idxs = [[] for _ in range(batch_size)]
            for row in number_indices: # row = [batch_idx, number idx]
                formatted_num_idxs[row[0]].append(row[1])
            
            for i in range(len(formatted_num_idxs)):
                formatted_num_idxs[i] = torch.tensor(formatted_num_idxs[i])

            padded_num_idxs = pad_sequence(formatted_num_idxs,
                                            batch_first=True,
                                            padding_value = -1)
            
            # create mask on indices
            number_mask = padded_num_idxs != -1
            print(f"number_mask {number_mask}")
            clamped_number_indices = padded_num_idxs.masked_fill(~number_mask, 0).type(torch.int64)
            

            if number_mask.size(1) > 0:
                # Shape: (batch_size, # of numbers in the passage, encoding_dim)
                encoded_numbers = torch.gather(
                    encoded_passage_for_numbers,
                    1,
                    clamped_number_indices.unsqueeze(-1).expand(
                        -1, -1, encoded_passage_for_numbers.size(-1)
                    ),
                )

                print(clamped_number_indices)
                print(clamped_number_indices.unsqueeze(-1).expand(\
                        -1, -1, encoded_passage_for_numbers.size(-1)\
                    ).size())

                
                # Shape: (batch_size, # of numbers in the passage)
                encoded_numbers = torch.cat(
                    [
                        encoded_numbers,
                        passage_vector_rep.unsqueeze(1).repeat(1, encoded_numbers.size(1), 1),
                    ],
                    -1,
                )
                

                # Shape: (batch_size, # of numbers in the passage, 3)
                number_sign_logits = self.number_sign_predictor(encoded_numbers)
                # print(number_sign_logits)
                number_sign_log_probs = torch.nn.functional.log_softmax(number_sign_logits, -1)
                

                # Shape: (batch_size, # of numbers in passage).
                best_signs_for_numbers = torch.argmax(number_sign_log_probs, -1)
                # For padding numbers, the best sign masked as 0 (not included).
                best_signs_for_numbers = best_signs_for_numbers.masked_fill(~number_mask, 0)
                print(f"best_signs_for_numbers: {best_signs_for_numbers}")
            
            else: 
                print("No number in the batch")


            pass

        
                
        pass


if __name__ == "__main__":
    test = True

    if test:
        torch.manual_seed(22)
        np.random.seed(239)
        device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")
        wemb_vocab_size = 5000
        number_emb_idxs = np.random.default_rng().choice(np.arange(1, wemb_vocab_size), size = int(wemb_vocab_size/10), replace = False)
        wemb_dim = 300
        cemb_vocab_size = 94
        cemb_dim = 64
        d_model = 128
        batch_size = 4
        q_max_len = 6
        c_max_len = 10
        char_dim = 16

        # fake embedding
        wv_tensor = torch.rand(wemb_vocab_size, wemb_dim)
        cv_tensor = torch.rand(cemb_vocab_size, cemb_dim)

        # fake input
        question_lengths = torch.LongTensor(batch_size).random_(1, q_max_len)
        question_wids = torch.zeros(batch_size, q_max_len).long()
        question_cids = torch.zeros(batch_size, q_max_len, char_dim).long()
        context_lengths = torch.LongTensor(batch_size).random_(1, c_max_len)
        context_wids = torch.zeros(batch_size, c_max_len).long()
        context_cids = torch.zeros(batch_size, c_max_len, char_dim).long()
        for i in range(batch_size):
            question_wids[i, 0:question_lengths[i]] = \
                torch.LongTensor(1, question_lengths[i]).random_(
                    1, wemb_vocab_size)
            question_cids[i, 0:question_lengths[i], :] = \
                torch.LongTensor(1, question_lengths[i], char_dim).random_(
                    1, cemb_vocab_size)
            context_wids[i, 0:context_lengths[i]] = \
                torch.LongTensor(1, context_lengths[i]).random_(
                    1, wemb_vocab_size)
            context_cids[i, 0:context_lengths[i], :] = \
                torch.LongTensor(1, context_lengths[i], char_dim).random_(
                    1, cemb_vocab_size)

        number_indices = np.argwhere((np.isin(context_wids.numpy(),number_emb_idxs)))

        # define model
        model = NAQANet(device, wv_tensor, cv_tensor)

        p1, p2 = model(context_wids, context_cids,
                       question_wids, question_cids, number_indices)
        print(f"p1 {p1}")
        print(f"p2 {p2}")
        print(torch.sum(p1, dim=1))
        print(torch.sum(p2))

        yp1 = torch.argmax(p1, 1)
        yp2 = torch.argmax(p2, 1)
        yps = torch.stack([yp1, yp2], dim=1)
        print(f"yp1 {yp1}")
        print(f"yp2 {yp2}")
        print(f"yps {yps}")

        ymin, _ = torch.min(yps, 1)
        ymax, _ = torch.max(yps, 1)
        print(f"ymin {ymin}")
        print(f"ymax {ymax}")