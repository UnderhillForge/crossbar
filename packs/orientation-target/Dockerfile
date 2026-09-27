FROM python:3.12-alpine

ENV PYTHONDONTWRITEBYTECODE=1
ENV FLAG_OPEN=1

WORKDIR /lab
COPY server.py /lab/server.py
COPY www /lab/www

USER nobody
EXPOSE 8080

CMD ["python", "/lab/server.py"]
