from simuleval.utils import entrypoint
from simuleval.agents import TextToTextAgent
from simuleval.agents.actions import ReadAction, WriteAction
from argparse import Namespace, ArgumentParser
import random

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

from transformers import FalconForCausalLM
from peft import LoraConfig, AutoPeftModelForCausalLM

from transformers.generation.stopping_criteria import StoppingCriteria
from llmsimul.falcon.falcon_stopping_criteria import StopTokenAndMaxLengthCriteria

@entrypoint
class FalconWaitkTextAgent(TextToTextAgent):
    def __init__(self, args: Namespace):
        super().__init__(args)
        self.waitk = args.waitk
        self.decoding_strategy = args.decoding_strategy
       
        # assuming PEFT-based model for now, since full-model fine-tuning is much rarer for low resource platforms
        #self.model = AutoPeftModelForCausalLM.from_pretrained(args.model_path, device_map="auto", trust_remote_code=True)
        bnb_config = BitsAndBytesConfig(
            load_in_4bit=True,
            bnb_4bit_quant_type="nf4",
            bnb_4bit_compute_dtype="float16",
            bnb_4bit_use_double_quant=False,
        )

        self.model = FalconForCausalLM.from_pretrained(
                'ybelkada/falcon-7b-sharded-bf16',
                device_map="auto",
                trust_remote_code=True,
                quantization_config=bnb_config,
        )
        self.model.load_adapter(args.model_path)
        self.tokenizer = AutoTokenizer.from_pretrained('ybelkada/falcon-7b-sharded-bf16', trust_remote_code=True)
        self.tokenizer.add_special_tokens({'pad_token': '[PAD]'})
        self.model.resize_token_embeddings(len(self.tokenizer))
        self.eoseq_ids = self.tokenizer("}", return_tensors="pt").input_ids.to('cuda')
        
    @staticmethod
    def add_args(parser: ArgumentParser):
        parser.add_argument("--waitk", type=int, default=3)
        parser.add_argument("--decoding-strategy", type=str, default="greedy")
        parser.add_argument("--model-path", type=str, required=True,
                                help="path to your pretrained model or PEFT augmentation.")

    def policy(self):
        lagging = len(self.states.source) - len(self.states.target)

        if lagging >= self.waitk or self.states.source_finished:
            current_source = " ".join(self.states.source)
            current_target = " ".join(self.states.target)

            if self.decoding_strategy == "greedy":
                model_output = self.make_inference_translation(current_source, current_target)
                prediction = self.find_string_between_symbols(model_output, '{', '}')
            else:
                raise NotImplementedError


            prediction_send = prediction
            if prediction == "<|endoftext|>":
                prediction_send = ""
           

            if prediction == "<|endoftext|>" or lagging < -5:
                print(f"Finished sentence: \n\tSource: {current_source}\n\t Target: {current_target + ' ' + prediction}", flush=True)


            # will need to modify finish condition at a later date
            return WriteAction(prediction_send, finished=(prediction == "<|endoftext|>" or lagging < -5))
        else:
            return ReadAction()



    ''' 
    The following functions are entirely the design of Max Wild and detail translation wrappers for Falcon 
    '''
    def make_inference_translation(self, source, current_translation):
        """
        Call upon a specific model to do a translation request, the model input
        format is defined in this function
        """

        if current_translation is None:
            current_translation = ''

        input_prompt = f'<human>: Given the English sentence {{{source}}}, and the current translation in Spanish {{{current_translation}}}, what\'s the next translated word? <assistant>: '

        # 8 max tokens seems to be a pretty safe value to catch multi-token words
        # reducing this results in possibly losing tokens, custom stopping criteria
        # presents a nice alternative that should be efficient
        encoding = self.tokenizer(input_prompt, return_tensors="pt")
        stopping_criteria = StopTokenAndMaxLengthCriteria(
            start_length=encoding.input_ids.shape[-1],
            max_new_tokens=15,
            eoseq_ids=self.eoseq_ids,
        )

        with torch.inference_mode():
            outputs = self.model.generate(
                input_ids = encoding.input_ids.to('cuda'),
                attention_mask = encoding.attention_mask,
                use_cache=True,
                stopping_criteria=[stopping_criteria],
                max_new_tokens=15,
                pad_token_id=self.tokenizer.pad_token_id,
            )

        all_output = self.tokenizer.decode(outputs[0])

        # Slice the returned array to remove the input that we fed the model
        return all_output[len(input_prompt) - 1:]


    # may not need to call this, 99% of the time the first and last token will be '{' and '}'
    def find_string_between_symbols(self, source, start_symbol='{', end_symbol='}'):
        """
        Returns the content between the first instance of the start symbol and
        end symbol, where the depth of the level of the symbols is maintained
            - so "{{in}in{in}in} {out}" returns "{in}in{in}in", not "{in"
                  ^            ^
                  First instance of start/end
        """

        content_inside = ''
        start_symbols_found = 0
        found_initial_start_symbol = False

        for i in range(len(source)):

            if source[i] == start_symbol:
                found_initial_start_symbol = True
                start_symbols_found += 1

            if found_initial_start_symbol:
                content_inside += source[i]

            if found_initial_start_symbol and source[i] == '}':
                start_symbols_found -= 1
                if start_symbols_found <= 0:
                    break

        return content_inside[1:-1]  # Ignore the initial and ending '{' and '}'
