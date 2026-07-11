FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir requests paho-mqtt aioesphomeapi \
    opencv-python-headless numpy
COPY scripts/meter_reader.py scripts/feedback.py scripts/
COPY scripts/ocr/*.py scripts/ocr/model.npz scripts/ocr/
CMD ["python", "-u", "scripts/meter_reader.py"]
