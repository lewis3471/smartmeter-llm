ARG BUILD_FROM
FROM $BUILD_FROM
RUN apk add --no-cache python3 py3-pip && \
    pip3 install --no-cache-dir --break-system-packages requests paho-mqtt aioesphomeapi
COPY meter_reader.py /app/scripts/meter_reader.py
COPY run.sh /run.sh
RUN chmod +x /run.sh
WORKDIR /app
CMD ["/run.sh"]
