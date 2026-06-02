# Builds PotreeConverter 2.x + GDAL, then runs the Node worker.
FROM ubuntu:22.04 AS build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    git cmake build-essential libtbb-dev && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/potree/PotreeConverter.git /src
WORKDIR /src
RUN mkdir build && cd build && cmake .. && make -j$(nproc)
# Build outputs: the PotreeConverter binary AND its shared libs (liblaszip.so etc.)
# all land in /src/build. We copy the whole build dir so nothing is missed.

FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    curl ca-certificates gdal-bin libtbb-dev \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Copy the entire PotreeConverter build output (binary + shared libraries)
COPY --from=build /src/build /opt/potree
# Put the binary on PATH and tell the linker where its .so files live
RUN ln -s /opt/potree/PotreeConverter /usr/local/bin/PotreeConverter
ENV LD_LIBRARY_PATH=/opt/potree

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY index.js ./
ENV PORT=8080
EXPOSE 8080
CMD ["node", "index.js"]
