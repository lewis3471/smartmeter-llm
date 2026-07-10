FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir requests paho-mqtt aioesphomeapi
COPY scripts/meter_reader.py scripts/
CMD ["python", "-u", "scripts/meter_reader.py"]
