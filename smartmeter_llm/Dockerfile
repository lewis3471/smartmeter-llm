ARG BUILD_FROM
FROM $BUILD_FROM
RUN apk add --no-cache python3 py3-pip py3-numpy py3-opencv git openssh-client rsync && \
    pip3 install --no-cache-dir --break-system-packages requests paho-mqtt aioesphomeapi
COPY meter_reader.py /app/scripts/meter_reader.py
COPY feedback.py /app/scripts/feedback.py
COPY nuc_feedback_sync.py /app/scripts/nuc_feedback_sync.py
COPY ocr/ /app/scripts/ocr/
COPY run.sh /run.sh
RUN chmod +x /run.sh
WORKDIR /app
CMD ["/run.sh"]
