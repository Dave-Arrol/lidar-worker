# Builds PotreeConverter + GDAL + PDAL + Python analysis stack, then runs the Node worker.
FROM ubuntu:22.04 AS build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    git cmake build-essential libtbb-dev && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 https://github.com/potree/PotreeConverter.git /src
WORKDIR /src
RUN mkdir build && cd build && cmake .. && make -j$(nproc)

FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y \
    curl ca-certificates bzip2 gdal-bin libtbb-dev \
    python3 python3-pip python3-dev build-essential swig cmake \
    && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# PDAL with COPC support.
# Ubuntu 22.04's apt PDAL (~2.0) predates the COPC reader (added in PDAL 2.4),
# so install a current PDAL from conda-forge via micromamba into an isolated
# prefix and append it to PATH. copc_clip.py shells out to the `pdal` CLI;
# PDAL's own HTTP/S3 support handles range-reads of remote COPC files.
RUN curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest \
        | tar -xvj -C /usr/local bin/micromamba
ENV MAMBA_ROOT_PREFIX=/opt/conda
RUN /usr/local/bin/micromamba create -y -p /opt/pdal -c conda-forge pdal \
    && /usr/local/bin/micromamba clean -a -y
ENV PATH=$PATH:/opt/pdal/bin

# Python analysis libraries (CSF = cloth-simulation-filter ground filter)
# Full algorithmic-pipeline stack (segmentation needs scikit-image; dbh/stem need
# scikit-learn + pandas; plots need matplotlib, headless via MPLBACKEND=Agg)
RUN pip3 install --no-cache-dir \
    "laspy[lazrs]==2.7.0" cloth-simulation-filter==1.1.7 \
    numpy scipy scikit-image scikit-learn pandas matplotlib rasterio
ENV MPLBACKEND=Agg

# PotreeConverter (binary + its shared libs) — worker calls it by absolute path.
# Kept for the existing opt-in octree path; can be dropped once COPC streaming
# fully replaces octree viewing (slims the image and speeds the build).
COPY --from=build /src/build /opt/potree
ENV LD_LIBRARY_PATH=/opt/potree

WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY index.js registry.js ./
COPY scripts ./scripts
ENV PORT=8080
EXPOSE 8080
CMD ["node", "index.js"]
