import spacy
from datetime import datetime
from gamechangerml.src.text_handling.process import preprocess
import numpy as np
import re
import pandas as pd
from tqdm import tqdm
import logging
import os
from elasticsearch import Elasticsearch
import xgboost as xgb
import requests
import json
from sklearn.preprocessing import LabelEncoder
from gamechangerml import MODEL_PATH, DATA_PATH
import typing as t
import base64
from urllib.parse import urljoin


ES_INDEX = os.environ.get("ES_INDEX", "gamechanger")


class ESUtils:
    def __init__(
        self,
        host: str = os.environ.get("ES_HOST", "localhost"),
        port: str = os.environ.get("ES_PORT", 443),
        user: str = os.environ.get("ES_USER", ""),
        password: str = os.environ.get("ES_PASSWORD", ""),
        enable_ssl: bool = os.environ.get(
            "ES_ENABLE_SSL", "True").lower() == "true",
        enable_auth: bool = os.environ.get(
            "ES_ENABLE_AUTH", "False").lower() == "true",
    ):

        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.enable_ssl = enable_ssl
        self.enable_auth = enable_auth

        self.auth_token = base64.b64encode(
            f"{self.user}:{self.password}".encode()
        ).decode()

    @property
    def client(self) -> Elasticsearch:
        if hasattr(self, "_client"):
            return getattr(self, "_client")

        host_args = dict(
            hosts=[
                {
                    "host": self.host,
                    "port": self.port,
                    "http_compress": True,
                    "timeout": 60,
                }
            ]
        )
        auth_args = (
            dict(http_auth=(self.user, self.password)
                 ) if self.enable_auth else {}
        )
        ssl_args = dict(use_ssl=self.enable_ssl)

        es_args = dict(
            **host_args,
            **auth_args,
            **ssl_args,
        )

        self._es_client = Elasticsearch(**es_args)
        return self._es_client

    @property
    def auth_headers(self) -> t.Dict[str, str]:
        return {"Authorization": f"Basic {self.auth_token}"} if self.enable_auth else {}

    @property
    def content_headers(self) -> t.Dict[str, str]:
        return {"Content-Type": "application/json"}

    @property
    def default_headers(self) -> t.Dict[str, str]:
        if self.enable_auth:
            return dict(**self.auth_headers, **self.content_headers)
        else:
            return dict(**self.content_headers)

    @property
    def root_url(self) -> str:
        return ("https" if self.enable_ssl else "http") + f"://{self.host}:{self.port}/"

    def request(self, method: str, url: str, **request_opts) -> requests.Response:
        complete_url = urljoin(self.root_url, url.lstrip("/"))
        return requests.request(
            method=method,
            url=complete_url,
            headers=self.default_headers,
            **request_opts,
        )

    def post(self, url: str, **request_opts) -> requests.Response:
        return self.request(method="POST", url=url, **request_opts)

    def put(self, url: str, **request_opts) -> requests.Response:
        return self.request(method="PUT", url=url, **request_opts)

    def get(self, url: str, **request_opts) -> requests.Response:
        return self.request(method="GET", url=url, **request_opts)

    def delete(self, url: str, **request_opts) -> requests.Response:
        return self.request(method="DELETE", url=url, **request_opts)


logger = logging.getLogger("gamechanger")

GC_USER_DATA = os.path.join(
    DATA_PATH, "user_data", "search_history", "SearchPdfMapping.csv"
)
LTR_MODEL_PATH = os.path.join(MODEL_PATH, "ltr")
LTR_DATA_PATH = os.path.join(DATA_PATH, "ltr")
os.makedirs(LTR_MODEL_PATH, exist_ok=True)
os.makedirs(LTR_DATA_PATH, exist_ok=True)


class LTR:
    def __init__(
        self,
        params={
            "max_depth": 6,
            "eta": 0.3,
            "objective": "rank:pairwise",
        },
    ):
        self.data = self.read_xg_data()
        self.params = params
        self.mappings = self.read_mappings()
        self.judgement = None
        self.eval_metrics = [
            "map",
            "map@25",
            "map@50",
            "map@75",
            "map@100",
            "ndcg@1",
            "ndcg@5",
            "ndcg@10",
            "ndcg@20",
            "ndcg@50",
            "ndcg@100",
            "rmse",
            "error",
        ]
        self.esu = ESUtils()

    def write_model(self, model):
        """write model: writes model to file
        params: model in json form
        returns:
        """
        # write model to json for LTR
        path = os.path.join(LTR_MODEL_PATH, "xgb-model.json")
        with open(path, "w") as output:
            output.write("[" + ",".join(list(model)) + "]")
            output.close()

    def read_xg_data(self, path=os.path.join(LTR_DATA_PATH, "xgboost.csv")):
        """read xg data: reads LTR formatted data
        params: path to file
        returns:
        """
        try:
            df = pd.read_csv(path)
            fts = df[df.columns[5:]]
            fts.index = df.qid

            label = df["ranking"]
            self.data = xgb.DMatrix(fts, label)
            return self.data
        except Exception as e:
            logger.error("Could not read in data for training")

    def read_mappings(self, path=GC_USER_DATA):
        """read mappings: reads search pdf mappings
        params: path to file
        returns:
            mappings file
        """
        mappings = None
        try:
            mappings = pd.read_csv(path)
        except Exception as e:
            logger.error("Could not read in mappings to make judgement list")
        return mappings

    def train(self, data=None, params=None, write=True):
        """train - train a xgboost model with parameters
        params:
            write: boolean to write to file
        returns:
            bst: xgboost object
            model: model json
        """
        if not data:
            data = self.data
        if not params:
            params = self.params
        bst = xgb.train(params, data)
        cv = xgb.cv(params, dtrain=data, nfold=3, metrics=self.eval_metrics)
        model = bst.get_dump(
            fmap=os.path.join(LTR_DATA_PATH, "featmap.txt"), dump_format="json"
        )
        if write:
            metadata = {}
            self.write_model(model)
            path = os.path.join(LTR_MODEL_PATH, "ltr_evals.csv")
            cv.to_csv(path, index=False)
            metadata["name"] = "ltr_model"
            metadata["evals"] = cv.mean().to_dict()
            metadata["params"] = params
            metadata["date"] = str(datetime.today())
            with open(os.path.join(LTR_MODEL_PATH, "metadata.json"), "w") as f:
                f.write(json.dumps(metadata))
        return bst, model

    def post_model(self, model, model_name):
        """post model - post a model to ES
        params:
            model: model in json form
            model_name: model name for ES
        returns:
            r: results
        """
        query = {
            "model": {
                "name": model_name,
                "model": {"type": "model/xgboost+json", "definition": model},
            }
        }
        endpoint = "/_ltr/_featureset/doc_features/_createmodel"
        r = self.esu.post(endpoint, data=json.dumps(query))
        return r.content

    def search(self, terms, rescore=True):
        """search: searches with a rescore with ltr option
        params:
            terms: search terms
            rescore: boolean
        returns:
            r: results
        """
        query = {
            "_source": {"includes": ["pagerank_r", "kw_doc_score_r"]},
            "stored_fields": ["filename", "title"],
            "from": 0,
            "size": 15,
            "query": {
                "bool": {
                    "must": [],
                    "should": [
                        {
                            "nested": {
                                "path": "paragraphs",
                                "inner_hits": {},
                                "query": {
                                    "bool": {
                                        "should": [
                                            {
                                                "query_string": {
                                                    "query": f"{terms}",
                                                    "default_field": "paragraphs.par_raw_text_t.gc_english",
                                                    "default_operator": "AND",
                                                    "fuzzy_max_expansions": 1000,
                                                    "fuzziness": "AUTO",
                                                    "analyzer": "gc_english",
                                                }
                                            }
                                        ]
                                    }
                                },
                            }
                        },
                        {
                            "multi_match": {
                                "query": f"{terms}",
                                "fields": ["display_title_s.search"],
                                "type": "phrase",
                                "operator": "and",
                                "boost": 4,
                            }
                        },
                        {"wildcard": {"keyw_5": {"value": f"*{terms}*"}}},
                        {
                            "wildcard": {
                                "display_title_s.search": {
                                    "value": f"*{terms}*",
                                    "boost": 6,
                                }
                            }
                        },
                    ],
                    "minimum_should_match": 1,
                    "filter": [{"term": {"is_revoked_b": "false"}}],
                }
            },
            "highlight": {
                "fields": {"display_title_s.search": {}, "keyw_5": {}, "id": {}},
                "fragmenter": "simple",
            },
            "sort": [{"_score": {"order": "desc"}}],
        }
        if rescore:
            query["rescore"] = {
                "query": {
                    "rescore_query": {
                        "sltr": {
                            "params": {"keywords": f"{terms}"},
                            "model": "lambda_rank1",
                        }
                    }
                }
            }
        r = self.esu.client.search(index=ES_INDEX, body=dict(query))
        return r

    def generate_judgement(self, mappings):
        """generate judgement - generates judgement list from user mapping data
        params:
            mappings: dataframe of user data extracted from pdf mapping table
        returns:
            count_df: cleaned dataframe with search mapped data
        """
        searches = mappings[["search", "document"]]
        searches.dropna(inplace=True)
        searches.search.replace("&quot;", "", regex=True, inplace=True)
        word_tuples = []
        for row in tqdm(searches.itertuples()):
            words = row.search.split(" ")
            clean_phr = re.sub(r"[^\w\s]", "", row.search)
            clean_phr = preprocess(clean_phr, remove_stopwords=True)
            if clean_phr:
                word_tuples.append((" ".join(clean_phr), row.document))

            for word in words:
                clean = word.lower()
                clean = re.sub(r"[^\w\s]", "", clean)
                clean = preprocess(clean, remove_stopwords=True)
                if clean:
                    tup = (clean[0], row.document)
                    word_tuples.append(tup)
        tuple_df = pd.DataFrame(word_tuples, columns=["search", "document"])
        count_df = pd.DataFrame()
        for keyword in tuple_df.search.unique():
            a = tuple_df[tuple_df.search == keyword]
            tmp_df = a.groupby("document").count()
            tmp_df["keyword"] = keyword
            count_df = count_df.append(tmp_df)
        count_df.sort_values("search")
        arr = count_df.search.copy()
        count_df["ranking"] = self.normalize(arr)
        count_df.ranking = count_df.ranking.apply(np.ceil)
        count_df.ranking = count_df.ranking.astype(int)
        le = LabelEncoder()
        count_df["qid"] = le.fit_transform(count_df.keyword)
        self.judgement = count_df

        return count_df

    def query_es_fts(self, df):
        """query ES features: gets ES feature logs from judgement list
        params:
            df: dataframe of judgement list and keyword
        returns:
            ltr_log: logs of from ES
        """
        ltr_log = []
        logger.info("querying es ltr logs")
        # loop through all unique keywords
        query_list = []
        for kw in tqdm(df.keyword.unique()):
            # get frame of all of the keyword rows
            tmp = df[df.keyword == kw]
            # get logged feature

            for docs in tmp.itertuples():
                doc = docs.Index
                q = self.construct_query(doc, kw)
                query_list.append(json.dumps({"index": ES_INDEX}))
                query_list.append(json.dumps(q))
        query = "\n".join(query_list)
        res = self.esu.client.msearch(body=query)
        ltr_log = [x["hits"]["hits"] for x in res["responses"]]
        return ltr_log

    def process_ltr_log(self, ltr_log, num_fts=8):
        """process ltr log: extracts features from ES logs for judgement list
        params:
            ltr_log: results from ES
            num_fts: number of features
        returns:
            all_vals: all logged features in matrix
        """
        all_vals = []
        logger.info("processing logs")
        for entries in ltr_log:
            if len(entries) > 0:
                # loop through entry logs (num of features)
                fts = []
                for entry in entries[0]["fields"]["_ltrlog"][0]["log_entry1"]:
                    # checks if entry is empty
                    if "value" in entry:
                        fts.append(entry["value"])
                    else:
                        fts.append(0)
                all_vals.append(fts)
            # if the doc doesnt exist then add all 0s
            else:
                all_vals.append(np.zeros(num_fts))
        return all_vals

    def generate_ft_txt_file(self, df):
        """generate feature text file: creates the LTR formatted training data
        params:
            df: dataframe of the judgement list with features
        returns:
            outputs a file
        """
        ltr_log = self.query_es_fts(df)
        vals = self.process_ltr_log(ltr_log)
        ft_df = pd.DataFrame(
            vals,
            columns=[
                "title",
                "keyw_5",
                "topics",
                "entities",
                "textlength",
                "paragraph",
                "popscore",
                "paragraph-phrase",
            ],
        )
        df.reset_index(inplace=True)
        df = pd.concat([df, ft_df], axis=1)

        logger.info("generating csv file")
        df.to_csv(os.path.join(LTR_DATA_PATH, "xgboost.csv"), index=False)
        return df

    def construct_query(self, doc, kw):
        """construct query: constructs query for logging features from es
        params:
            doc: document name that is in corpus
            kw: keyword to search on
        returns: query
        """
        query = {
            "_source": ["filename", "fields"],
            "query": {
                "bool": {
                    "filter": [
                        {"terms": {"filename": [doc]}},
                        {
                            "sltr": {
                                "_name": "logged_featureset",
                                "featureset": "doc_features",
                                "params": {"keywords": kw},
                            }
                        },
                    ]
                }
            },
            "ext": {
                "ltr_log": {
                    "log_specs": {
                        "name": "log_entry1",
                        "named_query": "logged_featureset",
                    }
                }
            },
        }
        return query

    def post_features(self):
        """post features: post features to es"""
        query = {
            "featureset": {
                "name": "doc_features",
                "features": [
                    {
                        "name": "title",
                        "params": ["keywords"],
                        "template_language": "mustache",
                        "template": {
                            "wildcard": {
                                "display_title_s.search": {
                                    "value": "*{{keywords}}*",
                                    "boost": 2,
                                }
                            }
                        },
                    },
                    {
                        "name": "keyw_5",
                        "params": ["keywords"],
                        "template_language": "mustache",
                        "template": {"match": {"keyw_5": "{{keywords}}"}},
                    },
                    {
                        "name": "textlength",
                        "params": ["keywords"],
                        "template_language": "mustache",
                        "template": {
                            "function_score": {
                                "functions": [
                                    {
                                        "field_value_factor": {
                                            "field": "page_count",
                                            "missing": 0,
                                        }
                                    }
                                ],
                                "query": {"match_all": {}},
                            }
                        },
                    },
                    {
                        "name": "paragraph",
                        "params": ["keywords"],
                        "template_language": "mustache",
                        "template": {
                            "nested": {
                                "path": "paragraphs",
                                "inner_hits": {},
                                "query": {
                                    "bool": {
                                        "should": [
                                            {
                                                "query_string": {
                                                    "query": "{{keywords}}",
                                                    "default_field": "paragraphs.par_raw_text_t.gc_english",
                                                    "default_operator": "AND",
                                                    "fuzzy_max_expansions": 1000,
                                                    "fuzziness": "AUTO",
                                                    "analyzer": "gc_english",
                                                }
                                            }
                                        ]
                                    }
                                },
                            }
                        },
                    },
                    {
                        "name": "popscore",
                        "params": ["keywords"],
                        "template_language": "mustache",
                        "template": {
                            "function_score": {
                                "functions": [
                                    {
                                        "field_value_factor": {
                                            "field": "pop_score",
                                            "missing": 0,
                                        }
                                    }
                                ],
                                "query": {"match_all": {}},
                            }
                        },
                    },
                    {
                        "name": "paragraph-phrase",
                        "params": ["keywords"],
                        "template_language": "mustache",
                        "template": {
                            "nested": {
                                "path": "paragraphs",
                                "inner_hits": {},
                                "query": {
                                    "bool": {
                                        "should": [
                                            {
                                                "match_phrase": {
                                                    "paragraphs.par_raw_text_t.gc_english": "{{keywords}}"
                                                }
                                            }
                                        ]
                                    }
                                },
                            }
                        },
                    },
                ],
            }
        }
        endpoint = "/_ltr/_featureset/doc_features"
        r = self.esu.post(endpoint, data=json.dumps(query))
        return r.content

    def post_init_ltr(self):
        endpoint = "/_ltr"
        r = self.esu.put(endpoint)
        return r.content

    def delete_ltr(self, model_name="ltr_model"):
        endpoint = f"/_ltr/_model/{model_name}"
        r = self.esu.delete(endpoint)
        return r.content

    def normalize(self, arr, start=0, end=4):
        """normalize: basic normalize between two numbers function
        params:
            arr: array to normalize
            start: beginning number integer
            end: ending number integer
        returns: normalized array
        """
        arr = np.log(arr)
        width = end - start
        res = (arr - arr.min()) / (arr.max() - arr.min()) * width + start
        return res
