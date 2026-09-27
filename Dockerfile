FROM python:3.13-slim
WORKDIR /app
COPY server.py LICENSE.txt ./
COPY public ./public
ENV PORT=8080 DATA_DIR=/data
EXPOSE 8080
CMD ["python", "server.py"]
