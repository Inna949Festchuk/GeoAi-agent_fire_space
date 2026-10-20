"""
Validation of "Strela" system data against independent ground truth:
- Rosleshoz 1T-ISM fire registry (detection: Precision/Recall/F1);
- NASA MCD64A1 burn maps (burn scars: IoU, area correlation).

Entry point: pipeline.run_validation() — used by the REST API
(/api/validation/), the chat tool run_validation and the MCP tool
run_validation.
"""