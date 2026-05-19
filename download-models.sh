#!/bin/bash
# 下载Hindsight所需的HuggingFace模型
# HuggingFace走代理，PyPI走国内镜像

set -e

echo "=========================================="
echo "下载Hindsight本地模型"
echo "=========================================="

# 设置代理
export https_proxy=http://127.0.0.1:10808
export http_proxy=http://127.0.0.1:10808
export HF_ENDPOINT=https://huggingface.co

echo "🌐 代理: http://127.0.0.1:10808"
echo "🔗 HuggingFace: 直接访问（走代理）"
echo ""

# 验证代理可用
echo "🔍 验证代理连通性..."
if curl -s --max-time 10 https://huggingface.co > /dev/null 2>&1; then
    echo "✅ HuggingFace可达"
else
    echo "❌ HuggingFace不可达，检查代理设置"
    exit 1
fi

echo ""
echo "📥 下载 Embedding 模型: BAAI/bge-small-en-v1.5 ..."
python3 -c "
from sentence_transformers import SentenceTransformer
print('开始下载 bge-small-en-v1.5...')
model = SentenceTransformer('BAAI/bge-small-en-v1.5')
print(f'下载完成! 维度: {model.get_sentence_embedding_dimension()}')
# 测试一下
emb = model.encode(['test'])
print(f'测试编码成功, shape: {emb.shape}')
"

echo ""
echo "📥 下载 Reranker 模型: cross-encoder/ms-marco-MiniLM-L-6-v2 ..."
python3 -c "
from sentence_transformers import CrossEncoder
print('开始下载 ms-marco-MiniLM-L-6-v2...')
model = CrossEncoder('cross-encoder/ms-marco-MiniLM-L-6-v2')
print('下载完成!')
# 测试一下
score = model.predict([['test query', 'test document']])
print(f'测试打分成功, score: {score}')
"

echo ""
echo "=========================================="
echo "✅ 模型下载完成！"
echo "=========================================="
echo "🧠 Embedding: BAAI/bge-small-en-v1.5 (384维)"
echo "🔍 Reranker: cross-encoder/ms-marco-MiniLM-L-6-v2"
echo ""
echo "下一步: bash ~/.hermes/hindsight/start-fixed.sh"
echo "=========================================="
