import os
import weaviate
from pathlib import Path
from llama_index.core import SimpleDirectoryReader, StorageContext, ServiceContext, VectorStoreIndex, load_index_from_storage
from llama_index.core import Settings
from llama_index.core.node_parser import SentenceSplitter, HierarchicalNodeParser, SentenceWindowNodeParser, get_leaf_nodes
from llama_index.core.embeddings import resolve_embed_model
from llama_index.core.indices.postprocessor import SentenceTransformerRerank, MetadataReplacementPostProcessor
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.retrievers import AutoMergingRetriever
from llama_index.vector_stores.weaviate import WeaviateVectorStore

embed_model_name = "local:/Users/Daglas/dalong.modelsets/bge-m3"
reranker_model_name = "/Users/Daglas/dalong.modelsets/bge-reranker-v2-m3"
Settings.embed_model = resolve_embed_model(embed_model_name)

def get_all_files_from_directory(directory_path):
    """获取指定目录下的所有文件路径
    
    Args:
        directory_path (str): 目录路径
    
    Returns:
        list: 包含所有文件路径的列表
    """
    path = Path(directory_path)
    if not path.exists() or not path.is_dir():
        raise ValueError(f"Invalid directory path: {directory_path}")
    
    return [str(file) for file in path.glob("*") if file.is_file()]

def create_document_index(input_files, index_name, chunk_size=1024, chunk_overlap=200):
    try:
        # 连接本地 Weaviate
        client = weaviate.connect_to_local()

        # 检查集合是否存在，如果存在则删除
        if client.collections.exists(index_name):
            client.collections.delete(index_name)
            print(f"Existing collection {index_name} has been deleted.")
        
        # 创建集合
        documents = client.collections.create(name=index_name)
        print("documents collection has been created.")

        # load documents
        documents = SimpleDirectoryReader(input_files=input_files).load_data()
        # 设置文档分割器
        splitter = SentenceSplitter(chunk_size=chunk_size, chunk_overlap=chunk_overlap)
        nodes = splitter.get_nodes_from_documents(documents)

        vector_store = WeaviateVectorStore(
            weaviate_client=client,
            index_name=index_name,
        )

        storage_context = StorageContext.from_defaults(vector_store=vector_store)

        index = VectorStoreIndex(
            nodes,
            storage_context=storage_context,
            show_progress=True  #显示进度
        )

        print("All vector data has been written to Weaviate.")

    except Exception as e:
        print(f"Error occurred: {str(e)}")
        raise
    finally:
        if 'client' in locals():
            client.close()  # Ensure client is always closed
            print("Weaviate connection closed.")

def delete_document_collection(index_name):
    """删除 Weaviate 中的集合"""
    # 连接本地 Weaviate
    client = weaviate.connect_to_local()
    
    # 删除集合
    client.collections.delete(index_name)

    print("documents collection has been deleted.")
    
    client.close()  # Free up resources


# the sentence window retrieval
def build_sentence_window_index(
    document, llm, embed_model=embed_model_name, save_dir="sentence_index"
):
    # create the sentence window node parser w/ default settings
    node_parser = SentenceWindowNodeParser.from_defaults(
        window_size=3,
        window_metadata_key="window",
        original_text_metadata_key="original_text",
    )
    sentence_context = ServiceContext.from_defaults(
        llm=llm,
        embed_model=embed_model,
        node_parser=node_parser,
    )
    if not os.path.exists(save_dir):
        sentence_index = VectorStoreIndex.from_documents(
            [document], service_context=sentence_context
        )
        sentence_index.storage_context.persist(persist_dir=save_dir)
    else:
        sentence_index = load_index_from_storage(
            StorageContext.from_defaults(persist_dir=save_dir),
            service_context=sentence_context,
        )

    return sentence_index


def get_sentence_window_query_engine(
    sentence_index,
    similarity_top_k=6,
    rerank_top_n=2,
):
    # define postprocessors
    postproc = MetadataReplacementPostProcessor(target_metadata_key="window")
    rerank = SentenceTransformerRerank(
        top_n=rerank_top_n, model="/Users/Daglas/dalong.modelsets/bge-reranker-v2-m3"
    )

    sentence_window_engine = sentence_index.as_query_engine(
        similarity_top_k=similarity_top_k, node_postprocessors=[postproc, rerank]
    )
    return sentence_window_engine

# for auto-merging retriever
def build_automerging_index(
    documents,
    llm,
    embed_model=embed_model_name,
    save_dir="merging_index",
    chunk_sizes=None,
):
    chunk_sizes = chunk_sizes or [2048, 512, 128]
    node_parser = HierarchicalNodeParser.from_defaults(chunk_sizes=chunk_sizes)
    nodes = node_parser.get_nodes_from_documents(documents)
    leaf_nodes = get_leaf_nodes(nodes)
    merging_context = ServiceContext.from_defaults(
        llm=llm,
        embed_model=embed_model,
    )
    storage_context = StorageContext.from_defaults()
    storage_context.docstore.add_documents(nodes)

    if not os.path.exists(save_dir):
        automerging_index = VectorStoreIndex(
            leaf_nodes, storage_context=storage_context, service_context=merging_context
        )
        automerging_index.storage_context.persist(persist_dir=save_dir)
    else:
        automerging_index = load_index_from_storage(
            StorageContext.from_defaults(persist_dir=save_dir),
            service_context=merging_context,
        )
    return automerging_index


def get_automerging_query_engine(
    automerging_index,
    similarity_top_k=12,
    rerank_top_n=2,
):
    base_retriever = automerging_index.as_retriever(similarity_top_k=similarity_top_k)
    retriever = AutoMergingRetriever(
        base_retriever, automerging_index.storage_context, verbose=True
    )
    rerank = SentenceTransformerRerank(
        top_n=rerank_top_n, model="/Users/Daglas/dalong.modelsets/bge-reranker-v2-m3"
    )
    auto_merging_engine = RetrieverQueryEngine.from_args(
        retriever, node_postprocessors=[rerank]
    )
    return auto_merging_engine