"""
Layer 1: CTP (Clinical Text Pre-Processor)

Extracts and cleans clinical text, de-identifies PHI, normalizes drug names.
"""

from typing import Any
from app.pipeline.base import BaseLayer
from app.utils import get_logger

logger = get_logger(__name__)


class Layer1CTP(BaseLayer):
    """
    Clinical Text Pre-Processor.

    Input: Raw clinical text
    Output: De-identified, normalized text with metadata
    """

    @property
    def layer_name(self) -> str:
        return "layer1_ctp"

    @property
    def prompt_filename(self) -> str:
        return f"layer1_ctp_v{self.settings.prompt_version}.md"

    def build_user_prompt(self, raw_text: str, **kwargs) -> str:
        """
        Build user prompt with the raw clinical text.

        Args:
            raw_text: The raw clinical text to process

        Returns:
            User prompt for the LLM
        """
        return f"""Process the following clinical text:

---
{raw_text}
---

Output STRICT JSON only."""

    def _extract_clean_course_text(self, output: dict) -> str | None:
        """
        Extract clean_course_text from various possible locations in the output.
        
        The LLM might return it in different fields or nested structures.
        """
        # Direct field
        if "clean_course_text" in output and isinstance(output["clean_course_text"], str):
            return output["clean_course_text"]
        
        # Alternative field names
        alternatives = [
            "course_text", "clinical_text", "text", "summary", 
            "cleaned_text", "processed_text", "content"
        ]
        for alt in alternatives:
            if alt in output and isinstance(output[alt], str) and len(output[alt]) > 50:
                return output[alt]
        
        # Nested under patient
        if "patient" in output and isinstance(output["patient"], dict):
            patient = output["patient"]
            if "clean_course_text" in patient:
                return patient["clean_course_text"]
            for alt in alternatives:
                if alt in patient and isinstance(patient[alt], str):
                    return patient[alt]
        
        # Try to find the longest string field (likely to be the course text)
        longest_text = ""
        for key, value in output.items():
            if isinstance(value, str) and len(value) > len(longest_text) and len(value) > 100:
                # Skip date-like or medication-like fields
                if not any(skip in key.lower() for skip in ["date", "medication", "drug", "dosage"]):
                    longest_text = value
        
        return longest_text if longest_text else None

    def _extract_dates(self, output: dict) -> dict:
        """
        Extract date information from various possible locations.
        """
        # Direct field
        if "extracted_dates" in output and isinstance(output["extracted_dates"], dict):
            return output["extracted_dates"]
        
        # Build from individual date fields
        dates = {}
        date_fields = {
            "admission": ["admission_date", "admission", "entry_date", "eintritt"],
            "discharge": ["discharge_date", "discharge", "exit_date", "austritt", "entlassung"],
            "operation": ["operation_date", "operation", "surgery_date", "op_date"],
        }
        
        for canonical, alternatives in date_fields.items():
            for alt in alternatives:
                if alt in output:
                    dates[canonical] = output[alt]
                    break
        
        # Check nested patient object
        if "patient" in output and isinstance(output["patient"], dict):
            patient = output["patient"]
            for canonical, alternatives in date_fields.items():
                if canonical not in dates:
                    for alt in alternatives:
                        if alt in patient:
                            dates[canonical] = patient[alt]
                            break
        
        # Default structure if empty
        if not dates:
            dates = {
                "admission": "[ADMISSION_DATE]",
                "discharge": "[DISCHARGE_DATE]",
                "operation": "[OPERATION_DATE]"
            }
        
        return dates

    def _extract_drug_normalization(self, output: dict) -> dict:
        """
        Extract drug normalization info from various possible locations.
        """
        # Direct field
        if "drug_normalization" in output and isinstance(output["drug_normalization"], dict):
            return output["drug_normalization"]
        
        # Check for medications field
        medications = None
        medication_fields = ["medications", "drugs", "medications_at_discharge", "therapy"]
        
        for field in medication_fields:
            if field in output:
                medications = output[field]
                break
        
        # Check nested patient object
        if medications is None and "patient" in output and isinstance(output["patient"], dict):
            patient = output["patient"]
            for field in medication_fields:
                if field in patient:
                    medications = patient[field]
                    break
        
        # Build result
        if medications is not None:
            return {
                "performed": True,
                "medications": medications if isinstance(medications, list) else [],
                "notes": ""
            }
        
        # Default
        return {"performed": False}

    def normalize_output(self, output: dict) -> dict:
        """
        Normalize the LLM output to the expected schema.
        
        This handles cases where the LLM returns a different structure
        but contains the required information in alternative locations.
        """
        normalized = {}
        
        # Extract clean_course_text
        clean_text = self._extract_clean_course_text(output)
        if clean_text:
            normalized["clean_course_text"] = clean_text
        
        # Extract dates
        normalized["extracted_dates"] = self._extract_dates(output)
        
        # Extract drug normalization
        normalized["drug_normalization"] = self._extract_drug_normalization(output)
        
        # Preserve any other fields from original output
        preserved_fields = ["notes", "warnings", "flags", "metadata"]
        for field in preserved_fields:
            if field in output:
                normalized[field] = output[field]
        
        return normalized

    def validate_output(self, output: dict) -> tuple[bool, str | None]:
        """
        Validate Layer 1 output schema with fallback normalization.

        First attempts to normalize the output if required fields are missing,
        then validates the normalized result.

        Required fields:
        - clean_course_text: str
        - extracted_dates: dict
        - drug_normalization: dict
        """
        required_fields = ["clean_course_text", "extracted_dates", "drug_normalization"]

        # Check if normalization is needed
        needs_normalization = False
        for field in required_fields:
            if field not in output:
                needs_normalization = True
                break
        
        # Try to normalize if needed
        if needs_normalization:
            logger.info(
                "layer1_output_normalization",
                message="Attempting to normalize non-standard output structure"
            )
            normalized = self.normalize_output(output)
            
            # Update output in place with normalized values
            for key, value in normalized.items():
                output[key] = value

        # Validate required fields
        for field in required_fields:
            if field not in output:
                return False, f"Missing required field: {field}"

        if not isinstance(output["clean_course_text"], str):
            return False, "clean_course_text must be a string"
        
        if len(output["clean_course_text"].strip()) == 0:
            return False, "clean_course_text cannot be empty"

        if not isinstance(output["extracted_dates"], dict):
            return False, "extracted_dates must be an object"

        if not isinstance(output["drug_normalization"], dict):
            return False, "drug_normalization must be an object"

        return True, None
