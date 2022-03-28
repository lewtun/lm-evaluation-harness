import os
import numpy as np
import transformers
from lm_eval.base import BaseLM
from lm_eval import utils
from tqdm import tqdm
import time


def get_result(response, ctxlen):
    """Process results from OpenAI API response.

    :param response: dict
        OpenAI API Response
    :param ctxlen: int
        Length of context (so we can slice them away and only keep the predictions)
    :return:
        continuation_logprobs: np.array
            Log probabilities of continuation tokens
        is_greedy: bool
            whether argmax matches given continuation exactly
    """
    is_greedy = True
    logprobs = response["logprobs"]["token_logprobs"][:-1]
    continuation_logprobs = sum(logprobs[ctxlen:])
    # print(logprobs[ctxlen:])

    for i in range(ctxlen, len(response["logprobs"]["tokens"][:-1])):
        token = response["logprobs"]["tokens"][:-1][i]
        top_tokens = response["logprobs"]["top_logprobs"][:-1][i]
        top_token = max(top_tokens.keys(), key=lambda x: top_tokens[x])
        if top_token != token:
            is_greedy = False
            break
    
    return continuation_logprobs, is_greedy


class _goose:
    choices: list

def oa_completion(**kwargs):
    """ Query OpenAI API for completion.

    Retry with back-off until they respond
    """
    import openai
    backoff_time = 3
    # print(kwargs)
    if len(kwargs["prompt"]) > 1 and isinstance(kwargs["prompt"], list):
        import dask
        res = []
        for pmpt in kwargs["prompt"]:
            k = kwargs.copy()
            k["prompt"] = [pmpt]
            res.append(dask.delayed(oa_completion)(**k))
        r = dask.compute(*res)
        ob = _goose()
        ob.choices = [x.choices[0] for x in r]

    while True:
        try:
            ret = openai.Completion.create(**kwargs)
            # print(ret.choices[0])
            return ret
        except openai.error.OpenAIError:
            import traceback
            traceback.print_exc()
            traceback.print_exc(file=os.path.join(os.environ["QUESTION_RESULT_PATH"], "traceback.txt"))
            time.sleep(backoff_time)
            backoff_time *= 1.5


class GPT3LM(BaseLM):
    REQ_CHUNK_SIZE = 20

    def __init__(self, engine, truncate=False, api_key=None, pass_strings=False):
        """

        :param engine: str
            OpenAI API engine (e.g. davinci)
        :param truncate: bool
            Truncate input if too long (if False and input is too long, throw error)
        """
        super().__init__()

        assert pass_strings, "so far, this branch only supports GooseAI, and won't work with the regular OpenAI api. this is mostly because there are still some remaining differences between the two apis that make this more complicated than just a drop in replacement. there's no fundamental reason why I couldn't support both on the same branch right now, but it would be a lot of work, and once gooseai finally makes their api conform to the openai api then we won't need this branch anymore and I'll implement something more simple once that does actually happen."

        import openai
        self.engine = engine
        print(self.max_length)
        self.tokenizer = transformers.GPT2TokenizerFast.from_pretrained('gpt2')
        self.pass_strings = pass_strings

        self.vocab_size = self.tokenizer.vocab_size

        # to make the annoying "Using pad_token, but it is not set yet." error go away
        self.tokenizer.pad_token = "<|endoftext|>"
        assert self.tokenizer.encode('hello\n\nhello') == [31373, 198, 198, 31373]
        self.truncate = truncate
        self.end_of_text_token_id = self.tokenizer.convert_tokens_to_ids(["<|endoftext|>"])[0]

        # Read from environment variable OPENAI_API_SECRET_KEY
        openai.api_key = api_key or os.environ["OPENAI_API_SECRET_KEY"]

    @property
    def eot_token_id(self):
        return self.tokenizer.eos_token_id

    @property
    def max_length(self):
        # Note: the OpenAI API supports up to 2049 tokens, with the first token being the first input token
        return 2048

    @property
    def max_gen_toks(self):
        return 256

    @property
    def batch_size(self):
        # Isn't used because we override _loglikelihood_tokens
        raise NotImplementedError()

    @property
    def device(self):
        # Isn't used because we override _loglikelihood_tokens
        raise NotImplementedError()

    def tok_encode(self, string: str):
        return self.tokenizer.encode(string, add_special_tokens=False)
    
    def tok_decode(self, tokens):
        return self.tokenizer.decode(tokens)

    def _loglikelihood_tokens(self, requests, disable_tqdm=False):
        res = []

        def _collate(x):
            # this doesn't efficiently handle last-token differences yet, but those are kinda annoying because
            # it's not guaranteed that the 100 or so logprobs we get to see actually contain all the continuations
            # we care about and so we need some kind of backup for when it isn't
            toks = x[1] + x[2]
            return -len(toks), tuple(toks)
        
        reord = utils.Reorderer(requests, _collate)

        for chunk in tqdm(list(utils.chunks(reord.get_reordered(), self.REQ_CHUNK_SIZE)), disable=disable_tqdm):
            inps = []
            ctxlens = []
            for cache_key, context_enc, continuation_enc in chunk:
                # max_length+1 because the API takes up to 2049 tokens, including the first context token
                inp = (context_enc + continuation_enc)[-(self.max_length+1):]
                # TODO: the logic is much simpler if we just look at the length of continuation tokens
                ctxlen = len(context_enc) - max(0, len(context_enc) + len(continuation_enc) - (self.max_length+1))

                # print(inp)
                if self.pass_strings:
                    inp = self.tok_decode(inp)
                inps.append(inp)
                ctxlens.append(ctxlen)
            response = None
            while True:
                try:
                    response = oa_completion(
                        engine=self.engine,
                        prompt=inps,
                        echo=True,
                        max_tokens=1,
                        logprobs=10,
                    )
                    break
                except Exception as e:
                    print(e)
                    print("pausing")
                    time.sleep(1)
                    continue

            for resp, ctxlen, (cache_key, context_enc, continuation_enc) in zip(response.choices, ctxlens, chunk):
                answer = get_result(resp, ctxlen)

                res.append(answer)

                # partial caching
                if cache_key is not None:
                    self.cache_hook.add_partial("loglikelihood", cache_key, answer)

        return reord.get_original(res)

    def greedy_until(self, requests):
        if not requests:
            return []
        res = []

        def _collate(x):
            toks = self.tok_encode(x[0])
            return len(toks), x[0]
        
        reord = utils.Reorderer(requests, _collate)

        def sameuntil_chunks(xs, size):
            ret = []
            lastuntil = xs[0][1]
            for x in xs:
                if len(ret) >= size or x[1] != lastuntil:
                    yield ret, lastuntil
                    ret = []
                    lastuntil = x[1]
                ret.append(x)
            
            if ret:
                yield ret, lastuntil

        # todo: more intelligent batching for heterogeneous `until`
        for chunk, until in tqdm(list(sameuntil_chunks(reord.get_reordered(), self.REQ_CHUNK_SIZE))):
            inps = []
            for context, _ in chunk:
                context_enc = self.tok_encode(context, max_length=self.max_length, truncation=False)
                inp = context_enc[-(self.max_length - self.max_gen_toks):]
                inps.append(self.tok_decode(inp))

            response = None
            while True:
                try:

                    response = oa_completion(
                        engine=self.engine,
                        prompt=inps,
                        max_tokens=self.max_gen_toks, 
                        temperature=0.,
                        # logprobs=10,
                        stop=until,
                    )

                    break
                except Exception as e:
                    print(e)
                    print("pausing")
                    time.sleep(1)
                    continue

            for resp, (context, until_) in zip(response.choices, chunk):
                s = resp['text']

                for term in until_:
                    s = s.split(term)[0]

                # partial caching
                self.cache_hook.add_partial("greedy_until", (context, until_), s)
                
                res.append(s)
        
        return reord.get_original(res)

    def _model_call(self, inps):
        # Isn't used because we override _loglikelihood_tokens
        raise NotImplementedError()

    def _model_generate(self, context, max_length, eos_token_id):
        # Isn't used because we override greedy_until
        raise NotImplementedError()


class GooseAILM(GPT3LM):
    def __init__(self, engine, truncate=False, api_key=None, force_pile_tokenizer=False):
        super().__init__(engine, truncate=truncate, api_key=api_key or os.environ["GOOSEAI_API_SECRET_KEY"], pass_strings=True)
        import openai
        openai.api_base = "https://api.goose.ai/v1"

        from best_download import download_file

        if engine == "gpt-neo-20b" or force_pile_tokenizer:
            download_file("http://eaidata.bmk.sh/data/pile_tokenizer.json", expected_checksum="d27f071586925d23ef1c4acdee28fb8bf5d99c4a9d638b4e3b08812e3eae6ee7", local_file="pile_tokenizer.json")
            self.tokenizer = transformers.PreTrainedTokenizerFast(tokenizer_file="pile_tokenizer.json")
        

    @property
    def max_length(self):
        # Note: this is temporary, will be raised to 2048 in the future
        return 2022

    @property
    def eot_token_id(self):
        return self.tokenizer.eos_token_id

    @property
    def max_gen_toks(self):
        return 64
