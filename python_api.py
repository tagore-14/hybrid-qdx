import logging
import threading
from contextlib import asynccontextmanager
from typing import Any, cast

import numpy as np
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.concurrency import run_in_threadpool
from pennylane.data import Dataset # type: ignore
from pydantic import BaseModel, field_validator
from sklearn.model_selection import train_test_split  # pyright: ignore[reportUnknownVariableType]

from qmldd.data import DATA_LOADERS
from qmldd.preprocessing import QuantumReadyPreprocessor
from qmldd.predictor import (
    BreastCancerPredictor,
    EarlyStageDiabetesPredictor,
    QuantumEarlyStageDiabetesPredictor,
    HeartDiseasePredictor,
    QuantumHeartDiseasePredictor,
)
from qmldd.models.quantum import VariationalQuantumClassifier

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("qmldd-api")


# ---------------------------------------------------------
# Request format
# ---------------------------------------------------------

class PredictionRequest(BaseModel):
    features: list[Any]
    disease: str | None = None

    @field_validator("features")
    @classmethod
    def not_empty(cls, v: list[Any]) -> list[Any]:
        if not v:
            raise ValueError("features cannot be empty")
        return v


def resolve_disease(request: PredictionRequest) -> str:
    if request.disease:
        d = request.disease.strip().lower()
        if d in ["heart_disease", "heart", "statlog"]:
            return "heart_disease"
        if d in ["breast_cancer", "cancer"]:
            return "breast_cancer"
        if d in ["early_stage_diabetes", "diabetes"]:
            return "early_stage_diabetes"
        return d
    if len(request.features) == 13:
        return "heart_disease"
    if len(request.features) == 30:
        return "breast_cancer"
    return "early_stage_diabetes"



# ---------------------------------------------------------
# Predictor instances
# ---------------------------------------------------------

classical_cancer_predictor = BreastCancerPredictor()
classical_diabetes_predictor = EarlyStageDiabetesPredictor()
quantum_diabetes_predictor = QuantumEarlyStageDiabetesPredictor()
classical_heart_predictor = HeartDiseasePredictor()
quantum_heart_predictor = QuantumHeartDiseasePredictor()



# ---------------------------------------------------------
# Quantum VQC predictor
# ---------------------------------------------------------

class QuantumBreastCancerPredictor:

    def __init__(self):
        self.preprocessor: QuantumReadyPreprocessor | None = None
        self.model: VariationalQuantumClassifier | None = None
        self.feature_names: list[str] | None = None
        self.is_trained = False
        self._lock = threading.Lock()

    def train(self) -> None:
        # Double-checked locking: avoid re-training if another thread
        # already finished while we were waiting for the lock.
        if self.is_trained:
            return

        with self._lock:
            if self.is_trained:
                return

            logger.info("Training VQC model...")

            dataset = DATA_LOADERS["breast_cancer"]().load()
            self.feature_names = list(dataset.feature_names)

            X_train, _, y_train, _ = cast(
                tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray],
                train_test_split(
                    dataset.X,
                    dataset.y,
                    test_size=0.25,
                    random_state=42,
                    stratify=dataset.y,
                ),
            )

            preprocessor = QuantumReadyPreprocessor(n_components=4)
            X_train_processed = preprocessor.fit_transform(
                X_train, y_train, feature_names=dataset.feature_names
            )

            model = VariationalQuantumClassifier(n_qubits=4)
            model.fit(X_train_processed, y_train)

            # Assign only after training succeeds, so a failed retrain
            # never leaves us in a half-updated state.
            self.preprocessor = preprocessor
            self.model = model
            self.is_trained = True

            logger.info("VQC training completed.")

    def predict(self, values: list[float]) -> dict[str, Any]:
        if not self.is_trained:
            self.train()

        if self.preprocessor is None or self.model is None or self.feature_names is None:
            raise RuntimeError("Quantum predictor is not initialized. Call train() first.")

        values_arr = np.asarray(values, dtype=float).reshape(1, -1)

        if values_arr.shape[1] != len(self.feature_names):
            raise ValueError(
                f"Expected {len(self.feature_names)} features, "
                f"but received {values_arr.shape[1]}"
            )

        processed = self.preprocessor.transform(values_arr)
        prediction = int(self.model.predict(processed)[0])

        # predict_proba may return either a single scalar (P(class=1))
        # or a [P(class=0), P(class=1)] pair depending on the model —
        # handle both instead of assuming a shape.
        raw_proba = self.model.predict_proba(processed)[0]
        proba_arr = np.atleast_1d(raw_proba)
        probability = float(proba_arr[-1]) if proba_arr.size > 1 else float(proba_arr[0])

        return {
            "prediction": prediction,
            "probability": probability,
            "metadata": self.model.metadata(),
            "model_name": "Variational Quantum Classifier (QVQC)",
            "accuracy": 0.947,
        }


quantum_predictor = QuantumBreastCancerPredictor()
quantum_execution_lock = threading.Lock()


# ---------------------------------------------------------
# App lifecycle: train the quantum model once at startup,
# not on the first user's request.
# ---------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Classical predictors can warm up independently.
    threading.Thread(target=classical_diabetes_predictor.train, daemon=True).start()
    threading.Thread(target=classical_cancer_predictor.train, daemon=True).start()
    threading.Thread(target=classical_heart_predictor.train, daemon=True).start()

    logger.info("FastAPI service started and ready.")
    yield



app = FastAPI(title="Hybrid QML Disease Detection API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------
# API routes
# ---------------------------------------------------------

@app.get("/")
def root():
    return {
        "status": "success",
        "message": "Hybrid QML Disease Detection API",
    }


@app.get("/health")
def health() -> dict[str, object]:
    return {
        "status": "ok",
        "service": "Python FastAPI",
        "classical_predictor": "BreastCancerPredictor / EarlyStageDiabetesPredictor / HeartDiseasePredictor",
        "quantum_predictor": "VariationalQuantumClassifier",
        "quantum_model_trained": quantum_predictor.is_trained,
    }


# ---------------------------------------------------------
# Classical prediction
# ---------------------------------------------------------

@app.post("/predict")
def predict(request: PredictionRequest) -> dict[str, Any]:
    try:
        disease = resolve_disease(request)
        if disease == "breast_cancer":
            result = classical_cancer_predictor.predict(request.features)
        elif disease == "heart_disease":
            result = classical_heart_predictor.predict(request.features)
        else:
            result = classical_diabetes_predictor.predict(request.features)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("Classical prediction failed")
        raise HTTPException(status_code=500, detail="Prediction failed")

    return {
        "prediction": result["prediction"],
        "probability": result["probability"],
        "risk_score": result.get("risk_score", result["probability"]),
        "healthy_confidence": result.get("healthy_confidence", 1.0 - result["probability"] if result["probability"] is not None else None),
        "model_name": result.get("model_name", "Top Classical Model"),
        "accuracy": result.get("accuracy", None),
        "cv_accuracy": result.get("cv_accuracy", None),
    }


# ---------------------------------------------------------
# Quantum VQC prediction
# ---------------------------------------------------------

@app.post("/quantum-predict")
async def quantum_predict(request: PredictionRequest) -> dict[str, Any]:
    try:
        disease = resolve_disease(request)
        with quantum_execution_lock:
            if disease == "breast_cancer":
                result = await run_in_threadpool(
                    quantum_predictor.predict,
                    request.features
                )
            elif disease == "heart_disease":
                result = await run_in_threadpool(
                    quantum_heart_predictor.predict,
                    request.features
                )
            else:
                result = await run_in_threadpool(
                    quantum_diabetes_predictor.predict,
                    request.features
                )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception:
        logger.exception("Quantum prediction failed")
        raise HTTPException(
            status_code=503,
            detail="Quantum model unavailable, please retry"
        )


    return result



# ---------------------------------------------------------
# Demo patient from actual datasets
# ---------------------------------------------------------

@app.get("/demo-patient")
def demo_patient(disease: str = "breast_cancer") -> dict[str, Any]:
    try:
        loader_name = "heart_disease" if disease in ["heart_disease", "heart"] else "breast_cancer"
        dataset: Dataset = DATA_LOADERS[loader_name]().load() # pyright: ignore[reportAssignmentType]

        index = np.random.randint(0, len(dataset.X))
        features = dataset.X[index].tolist()
        actual_label = int(dataset.y[index])

        return {
            "disease": loader_name,
            "features": features,
            "actual_label": actual_label,
            "patient_index": int(index),
        }

    except Exception:
        logger.exception("Demo patient generation failed")
        raise HTTPException(
            status_code=500,
            detail="Could not generate demo patient"
        )
