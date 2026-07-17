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
# PDAL (COPC reader/writer), untwine (out-of-core LAS/LAZ -> COPC builder), and the AWS
# CLI (S3 transfers), all from conda-forge via micromamba into an isolated prefix on PATH.
# Ubuntu 22.04's apt PDAL (~2.0) predates the COPC reader (added in PDAL 2.4); copc_clip.py
# shells out to `pdal`, and the /ingest path shells out to `aws` and `untwine`.
RUN curl -Ls https://micro.mamba.pm/api/micromamba/linux-64/latest \
        | tar -xvj -C /usr/local bin/micromamba
ENV MAMBA_ROOT_PREFIX=/opt/conda
# Left unpinned intentionally: conda-forge versions aren't verifiable here, and a
# wrong pin breaks the build. To freeze these too, run `micromamba list -p /opt/pdal`
# inside a build you've confirmed works and pin pdal/untwine/awscli to those.
RUN /usr/local/bin/micromamba create -y -p /opt/pdal -c conda-forge pdal untwine awscli \
    && /usr/local/bin/micromamba clean -a -y
ENV PATH=$PATH:/opt/pdal/bin

# Isolated env for the Harvest Planner. model.pkl is pickled under numpy 2.x +
# scikit-learn 1.8, which is incompatible with the LiDAR stack's pinned numpy
# 1.26 / sklearn 1.5 (PCG64 BitGenerator pickle format differs). Keeping it in
# its own env lets the model load in its native version family without touching
# the LiDAR toolchain. predict_coupe / extract_terrain / os_tiles run under this.
RUN /usr/local/bin/micromamba create -y -p /opt/harvest -c conda-forge \
        python=3.11 'numpy>=2,<3' scikit-learn=1.8.0 pandas pyproj \
    && /usr/local/bin/micromamba clean -a -y
# Python analysis libraries (CSF = cloth-simulation-filter ground filter)
# Full algorithmic-pipeline stack (segmentation needs scikit-image; dbh/stem need
# scikit-learn + pandas; plots need matplotlib, headless via MPLBACKEND=Agg).
# Versions are PINNED so rebuilds are reproducible: an unpinned build silently
# tracks "latest", which is how a rebuild can change behaviour (e.g. a numpy 2 /
# pandas 3 major bump, or a new lazrs LAZ backend) without any code change. This
# is a coherent numpy-1.x set; lazrs is pinned inside laspy 2.7.0's >=0.8,<0.9
# requirement. pyproj added for CRS detection (detect_crs / normalise were
# silently assuming EPSG:27700 without it); 3.6.1 bundles its own PROJ data,
# independent of the conda PDAL env's PROJ.
RUN pip3 install --no-cache-dir \
    "laspy[lazrs]==2.7.0" lazrs==0.8.1 cloth-simulation-filter==1.1.7 \
    numpy==1.26.4 scipy==1.13.1 scikit-image==0.24.0 scikit-learn==1.5.2 \
    pandas==2.2.3 matplotlib==3.9.2 rasterio==1.3.11 pyproj==3.6.1
ENV MPLBACKEND=Agg
# PotreeConverter (binary + its shared libs) — worker calls it by absolute path.
# Kept for the existing opt-in octree path; can be dropped once COPC streaming
# fully replaces octree viewing (slims the image and speeds the build).
COPY --from=build /src/build /opt/potree
ENV LD_LIBRARY_PATH=/opt/potree
WORKDIR /app
COPY package.json ./
RUN npm install --omit=dev
COPY index.js registry.js run-once.js harvest_quote.js ./
COPY scripts ./scripts
# Harvest Planner (feature #11): the 3 MB sklearn model + the validated v1
# predict tool. pandas/scikit-learn/numpy are already in the Python stack above.
COPY harvest ./harvest
ENV PORT=8080
EXPOSE 8080
CMD ["node", "index.js"]