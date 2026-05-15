FROM pytorch/pytorch:2.6.0-cuda12.4-cudnn9-runtime

RUN apt update && apt install -y \
    git wget curl unzip build-essential tmux \
    && apt clean

RUN pip install --upgrade pip && pip install \
    jupyterlab \
    nltk \
    'spacy[transformers]' \
    transformers \
    sentencepiece \
    sentence-transformers \
    pandas \
    numpy \
    matplotlib \
    seaborn \
    scikit-learn \
    chromadb \
    beautifulsoup4 \
    ebooklib \
    ftfy \
    unidecode \
    regex \
    more-itertools \
    tqdm \
    typer \
    rich

RUN python -m spacy download en_core_web_sm && \
    python -m spacy download en_core_web_trf

WORKDIR /workspace
EXPOSE 8888
CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser", "--allow-root", "--ServerApp.token=''", "--ServerApp.password=''"]
