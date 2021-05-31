import os
import torch
import json
import time
from bentoml import env, artifacts, api, BentoService
from bentoml.adapters import JsonInput
from bentoml.frameworks.pytorch import PytorchModelArtifact
from transformers.data.processors.squad import SquadResult, SquadV2Processor
from torch.utils.data import DataLoader, RandomSampler, SequentialSampler
from torch.utils.data.distributed import DistributedSampler
from transformers import squad_convert_examples_to_features
from transformers.data.metrics.squad_metrics import (
    compute_predictions_logits,
    squad_evaluate,
)
from fastprogress.fastprogress import master_bar, progress_bar
from attrdict import AttrDict
from src import (
    CONFIG_CLASSES,
    TOKENIZER_CLASSES,
    MODEL_FOR_QUESTION_ANSWERING,
    init_logger,
    set_seed
)

def to_list(tensor):
    return tensor.detach().cpu().tolist()

config_dir = 'config'
task = 'news'
config_file = 'deploy_model.json'

with open(os.path.join(config_dir, task, config_file)) as f:
    args = AttrDict(json.load(f))

args.output_dir = os.path.join(args.ckpt_dir, args.output_dir)

# gpu쓸거면 이거 해야함
#args.device = torch.device('cuda')
args.device = torch.device('cpu')

set_seed(args)

# 모델 불러오기
# config = CONFIG_CLASSES[args.model_type].from_pretrained(
#     args.model_name_or_path,
# )
tokenizer = TOKENIZER_CLASSES[args.model_type].from_pretrained(
    args.model_name_or_path,
    do_lower_case=args.do_lower_case,
)
# model = MODEL_FOR_QUESTION_ANSWERING[args.model_type].from_pretrained(
#     args.model_name_or_path,
#     config=config,
# )

# args.device = torch.device('cuda')
# model.to(args.device)

@env(infer_pip_packages=True)
@artifacts([PytorchModelArtifact('model')])
class SingleMRCModel(BentoService):
    """
    A minimum prediction service exposing a Scikit-learn model
    """
    @api(input=JsonInput(), batch=False)
    def predict(self, input_data):
        """
        An inference API named `predict` with Dataframe input adapter, which codifies
        how HTTP requests or CSV files are converted to a pandas Dataframe object as the
        inference API function input
        """
        model = self.artifacts.model
        model.to(args.device)
        # 데이터 불러오기
        with open(os.path.join(args.data_dir, args.task, args.predict_file), 'w') as f:
            json.dump(input_data, f)
        
        processor = SquadV2Processor()
        examples = processor.get_dev_examples(os.path.join(args.data_dir, args.task),
                                                      filename=args.predict_file)
        
        print(f'data load OK, number of exampels {len(examples)}')

        features, dataset = squad_convert_examples_to_features(
            examples=examples,
            tokenizer=tokenizer,
            max_seq_length=args.max_seq_length,
            doc_stride=args.doc_stride,
            max_query_length=args.max_query_length,
            is_training=False,
            return_dataset="pt",
            threads=1,
        )

        eval_sampler = SequentialSampler(dataset)
        eval_dataloader = DataLoader(dataset, sampler=eval_sampler, batch_size=args.eval_batch_size)
        all_results = []
        for batch in progress_bar(eval_dataloader):
            model.eval()
            batch = tuple(t.to(args.device) for t in batch)

            with torch.no_grad():
                inputs = {
                    "input_ids": batch[0],
                    "attention_mask": batch[1],
                    "token_type_ids": batch[2],
                }

                if args.model_type in ["xlm", "roberta", "distilbert", "distilkobert", "xlm-roberta"]:
                    del inputs["token_type_ids"]

                example_indices = batch[3]
                outputs = model(**inputs)
            
            for i, example_index in enumerate(example_indices):
                eval_feature = features[example_index.item()]
                unique_id = int(eval_feature.unique_id)

                output = [to_list(output[i]) for output in outputs.values()]

                # Some models (XLNet, XLM) use 5 arguments for their predictions, while the other "simpler"
                # models only use two.
                if len(output) >= 5:
                    start_logits = output[0]
                    start_top_index = output[1]
                    end_logits = output[2]
                    end_top_index = output[3]
                    cls_logits = output[4]

                    result = SquadResult(
                        unique_id,
                        start_logits,
                        end_logits,
                        start_top_index=start_top_index,
                        end_top_index=end_top_index,
                        cls_logits=cls_logits,
                    )

                else:
                    start_logits, end_logits = output
                    result = SquadResult(unique_id, start_logits, end_logits)
                all_results.append(result)
        
        current_time = str(time.strftime('%y-%m-%d %H:%M:%S'))
        output_prediction_file = os.path.join(args.output_dir, "predictions_{}.json".format(current_time))
        output_nbest_file = os.path.join(args.output_dir, "nbest_predictions_{}.json".format(current_time))
        
        if args.version_2_with_negative:
            output_null_log_odds_file = os.path.join(args.output_dir, "null_odds_{}.json".format(current_time))
        else:
            output_null_log_odds_file = None

        predictions = compute_predictions_logits(
            examples,
            features,
            all_results,
            args.n_best_size,
            args.max_answer_length,
            args.do_lower_case,
            output_prediction_file,
            output_nbest_file,
            output_null_log_odds_file,
            args.verbose_logging,
            args.version_2_with_negative,
            args.null_score_diff_threshold,
            tokenizer,
        )

        return predictions