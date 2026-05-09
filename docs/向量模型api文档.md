向量生成#Copy link

该接口用于获取 Serverless 支持的词向量，将任意中文或英文文本映射为稠密向量，用于语义检索、聚类或下游模型输入，该接口需要使用 Access Token 进行授权。

Body

required

application/json

- model

  Type:stringenum

  required

  模型名称，大小写不敏感并且支持带有命名空间，例如bge-m3及BAAI/bge-m3都可支持。

  - Qwen3-Embedding-0.6B
  - jina-clip-v1
  - jina-clip-v2
  - jina-embeddings-v4
  - bge-m3
  - Show all values

- input

  required

  One ofstring

  - Type:string

    输入待编码文本

- encoding_format

  Type:string

  default: 

  "float"

  向量数值格式

- dimensions

  Type:integer

  返回向量的维度；留空则使用模型默认值

- user

  Type:stringnullable

  唯一标识发送请求的最终用户

Responses

- application/json

Request Example forpost/embeddings

Selected HTTP client:Shell CurlLibcurlHttpClientRestSharpclj-httpHttpNewRequestHTTP/1.1AsyncHttpjava.net.httpOkHttpUnirestFetchAxiosofetchjQueryXHROkHttpFetchAxiosofetchundiciNSURLSessionCohttpcURLGuzzleInvoke-WebRequestInvoke-RestMethodhttp.clientRequestsHTTPX (Sync)HTTPX (Async)httrnet::httpreqwestCurlWgetHTTPieNSURLSession

Copy content

```curl
curl https://ai.gitee.com/v1/embeddings \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{
  "model": "Qwen3-Embedding-0.6B",
  "input": "",
  "encoding_format": "float",
  "dimensions": 1,
  "user": ""
}'
```

Test Request(post /embeddings)

Status:200

Copy content

```json
{
  "object": "list",
  "data": [
    {
      "object": "embedding",
      "embedding": [
        1
      ],
      "index": 1
    }
  ],
  "model": "string",
  "usage": {
    "prompt_tokens": 1,
    "total_tokens": 1
  }
}
```

向量结果

句子相似度对比#Copy link

该接口用于调用 Serverless 计算一段原文本与一组候选句子之间的语义相似度得分，用于排序、去重或检索。

Body

required

application/json

- model

  Type:stringenum

  required

  模型名称，大小写不敏感并且支持带有命名空间，例如bge-reranker-v2-m3及BAAI/bge-reranker-v2-m3都可支持。

  - bce-reranker-base_v1
  - bge-reranker-large
  - bge-reranker-v2-m3
  - all-mpnet-base-v2

- inputs

  Type:object

  required

  Show Child Attributesfor inputs

- normalize

  Type:boolean

  是否对输入文本进行统一归一化

Responses

- application/json

Request Example forpost/sentence-similarity

Selected HTTP client:Shell CurlLibcurlHttpClientRestSharpclj-httpHttpNewRequestHTTP/1.1AsyncHttpjava.net.httpOkHttpUnirestFetchAxiosofetchjQueryXHROkHttpFetchAxiosofetchundiciNSURLSessionCohttpcURLGuzzleInvoke-WebRequestInvoke-RestMethodhttp.clientRequestsHTTPX (Sync)HTTPX (Async)httrnet::httpreqwestCurlWgetHTTPieNSURLSession

Copy content

```curl
curl https://ai.gitee.com/v1/sentence-similarity \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{
  "model": "bce-reranker-base_v1",
  "inputs": {
    "source_sentence": "",
    "sentences": [
      ""
    ]
  },
  "normalize": true
}'
```

Test Request(post /sentence-similarity)

Status:200

Copy content

```json
[
  1
]
```

句子相似度对比结果

多模态重排接口#Copy link

该接口用于调用 Serverless 根据查询对多模态文档列表进行语义相关度重排，返回按得分降序排列的结果。

Body

required

application/json

- model

  Type:stringenum

  required

  模型名称，大小写不敏感并且支持带有命名空间，例如jina-reranker-m0。

  - clip-vit
  - Qwen3-VL-Reranker-2B
  - Qwen3-VL-Reranker-8B
  - jina-reranker-m0

- query

  required

  查询文档

  One oftext

  - text

    Type:string

    required

    文本内容

- documents

  Type:array object[]…25

  required

  待重排的目标文档列表

  Show Child Attributesfor documents

- return_documents

  Type:boolean

  default: 

  false

  是否在响应中原样返回文档内容

Responses

- application/json

Request Example forpost/rerank/multimodal

Selected HTTP client:Shell CurlLibcurlHttpClientRestSharpclj-httpHttpNewRequestHTTP/1.1AsyncHttpjava.net.httpOkHttpUnirestFetchAxiosofetchjQueryXHROkHttpFetchAxiosofetchundiciNSURLSessionCohttpcURLGuzzleInvoke-WebRequestInvoke-RestMethodhttp.clientRequestsHTTPX (Sync)HTTPX (Async)httrnet::httpreqwestCurlWgetHTTPieNSURLSession

Copy content

```curl
curl https://ai.gitee.com/v1/rerank/multimodal \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{
  "model": "clip-vit",
  "query": {
    "text": ""
  },
  "documents": [
    {
      "text": ""
    }
  ],
  "return_documents": false
}'
```

Test Request(post /rerank/multimodal)

Status:200

Copy content

```json
[
  {
    "index": 1,
    "document": {
      "text": "string"
    },
    "score": 1
  }
]
```

重新排序结果

句子重排#Copy link

该接口用于调用 Serverless 对一组候选句子与查询句的语义相关度进行打分并重新排序

Body

required

application/json

- model

  Type:stringenum

  required

  模型名称，大小写不敏感并且支持带有命名空间，例如bge-reranker-v2-m3及BAAI/bge-reranker-v2-m3都可支持。

  - bce-reranker-base_v1
  - Qwen3-Reranker-0.6B
  - Qwen3-Reranker-4B
  - bge-reranker-v2-m3
  - Qwen3-Reranker-8B

- query

  Type:string

  required

  查询句

- top_n

  Type:integer

  default: 

  3

  返回得分最高的前 N 条，默认为 3

- documents

  Type:array string[]

  required

  候选句列表

Responses

- application/json

Request Example forpost/rerank

Selected HTTP client:Shell CurlLibcurlHttpClientRestSharpclj-httpHttpNewRequestHTTP/1.1AsyncHttpjava.net.httpOkHttpUnirestFetchAxiosofetchjQueryXHROkHttpFetchAxiosofetchundiciNSURLSessionCohttpcURLGuzzleInvoke-WebRequestInvoke-RestMethodhttp.clientRequestsHTTPX (Sync)HTTPX (Async)httrnet::httpreqwestCurlWgetHTTPieNSURLSession

Copy content

```curl
curl https://ai.gitee.com/v1/rerank \
  --request POST \
  --header 'Content-Type: application/json' \
  --data '{
  "model": "bce-reranker-base_v1",
  "query": "",
  "top_n": 3,
  "documents": [
    ""
  ]
}'
```

Test Request(post /rerank)

Status:200

Copy content

```json
{
  "model": "string",
  "usage": {
    "total_tokens": 1,
    "prompt_tokens": 1
  },
  "results": [
    {
      "index": 1,
      "document": {
        "text": "string"
      },
      "relevance_score": 1
    }
  ]
}
```

重新排序结果