FROM pytorch/pytorch:2.2.0-cuda12.1-cudnn8-runtime

# Установка зависимостей
RUN apt update && apt install -y \
    git wget curl unzip build-essential \
    && apt clean

# Python библиотеки
RUN pip install --upgrade pip && pip install \
    jupyterlab \
    nltk \
    spacy \
    transformers \
    sentencepiece \
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

# Загрузка языковой модели spaCy
RUN python -m spacy download en_core_web_sm

# Рабочая директория
WORKDIR /workspace

EXPOSE 8888

CMD ["jupyter", "lab", "--ip=0.0.0.0", "--port=8888", "--no-browser", "--allow-root", "--ServerApp.token=''", "--ServerApp.password=''"]


