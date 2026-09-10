// QuantumDx Cloud & Local API Configuration
const envBase = import.meta.env.VITE_API_BASE_URL;

export const API_BASE_URL =
  envBase !== undefined && envBase !== ""
    ? envBase
    : import.meta.env.DEV
    ? "http://localhost:5000"
    : "";

export const STREAMLIT_URL =
  import.meta.env.VITE_STREAMLIT_URL || "http://localhost:8501";

export const STREAMLIT_EMBED_URL =
  `${STREAMLIT_URL}${STREAMLIT_URL.includes("?") ? "&" : "?"}embed=true`;
