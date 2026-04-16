FROM python:3.8-slim-bullseye

# fastodeint (the C++ ODE solver used when fast=True) was removed from GitHub
# by Quarticai and is no longer available. The pure Python scipy fallback
# (fast=False) is used instead — identical results, ~2x slower per batch.

WORKDIR /app

# Copy requirements and drop the unavailable fastodeint dependency
COPY requirements.txt .
RUN grep -v fastodeint requirements.txt > requirements_filtered.txt \
    && pip install --no-cache-dir -r requirements_filtered.txt \
    && rm requirements_filtered.txt

# Copy the rest of the project
COPY . .

# Install pensimpy in editable mode so examples resolve imports
RUN pip install --no-cache-dir --no-deps -e .

# API dependencies
RUN pip install --no-cache-dir fastapi uvicorn[standard]

EXPOSE 8000

CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
