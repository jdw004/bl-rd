# Normal submission builds validate committed artifacts; they do not run pytest.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS base
FROM base AS validation
COPY maps /app/maps
COPY selector.py dependency_map.py artifact_bundle.py validate_maps.py /app/
COPY unified_diff.py patch_analysis.py /app/
COPY selection.py /app/
COPY requirements.lock build_dependencies.py dependency_structure.py fetch_source.py process_dependencies.py /app/
RUN python /app/validate_maps.py --artifacts /app/maps \
    --requirements /app/requirements.lock --tools-root /app

# Validation/generation tools, evaluation labels, reports and loose baseline
# tests never enter the runtime stage. Baseline bytes remain inside the bundle
# because independent patch reconstruction needs them.
FROM base AS runtime
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 HOME=/tmp SKIP_INTEGRATION=0
COPY --from=validation --chmod=644 /app/maps/manifest.json /app/maps/dependencies.json.gz /app/maps/
COPY --from=validation --chmod=644 /app/selector.py /app/selection.py /app/dependency_map.py /app/artifact_bundle.py /app/unified_diff.py /app/patch_analysis.py /app/
RUN chmod 755 /app /app/maps
USER 65534:65534
CMD ["python", "/app/selector.py"]
