ARG BUILD_FROM
FROM ${BUILD_FROM}

RUN apk add --no-cache python3 py3-pip py3-lxml ca-certificates \
 && pip3 install --no-cache-dir --break-system-packages \
        upnpclient \
        websocket-client \
        paho-mqtt

COPY bridge.py /
COPY run.sh /
RUN chmod a+x /run.sh

CMD [ "/run.sh" ]
