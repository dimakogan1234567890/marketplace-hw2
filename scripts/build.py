"""Generate server handlers and DTOs from the contract; never edit build/ by hand."""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
VERSION = "7.14.0"
URL = f"https://repo.maven.apache.org/maven2/org/openapitools/openapi-generator-cli/{VERSION}/openapi-generator-cli-{VERSION}.jar"


def special_type(schema):
    """Fix format mappings that the stock beta generator does not preserve."""
    fmt = schema.get("format")
    if fmt == "decimal":
        limits = []
        for source, target in [("minimum", "ge"), ("maximum", "le"), ("multipleOf", "multiple_of")]:
            if source in schema:
                limits.append(f"{target}=Decimal('{schema[source]}')")
        return f"Annotated[Decimal, BeforeValidator(number_only), Field({', '.join(limits)})]"
    if fmt == "uuid":
        return "UUID"
    if fmt == "date-time":
        return "Annotated[AwareDatetime, BeforeValidator(datetime_only)]"
    return None


def main():
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    spec = json.loads((ROOT / "openapi/marketplace.json").read_text("utf-8"))
    for schema in spec["components"]["schemas"].values():
        schema["x-forbid-extra"] = schema.get("additionalProperties") is False
        for name, field in schema.get("properties", {}).items():
            annotation = special_type(field)
            if annotation:
                if field.get("nullable"):
                    annotation = f"Optional[{annotation}]"
                if name not in schema.get("required", []):
                    annotation += " = None"
                field["x-contract-type"] = annotation
    for path in spec["paths"].values():
        for operation in path.values():
            if not isinstance(operation, dict) or "operationId" not in operation:
                continue
            operation["x-success-code"] = next(int(code) for code in operation["responses"] if code.startswith("2"))
    enriched = build / "generator-input.json"
    enriched.write_text(json.dumps(spec, ensure_ascii=False, indent=2), "utf-8")
    generated = (build / "generated").resolve()
    assert generated.parent == build.resolve()
    if generated.exists():
        shutil.rmtree(generated)
    jar = Path(os.getenv("OPENAPI_GENERATOR_JAR", ROOT / ".tools/openapi-generator-cli.jar"))
    if not jar.exists():
        jar.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(URL, jar)
        expected = "e03186835022ca02da4aa95e3967b6a3b6d44c2e5f7606e6d5c22466f519c757"
        if hashlib.sha256(jar.read_bytes()).hexdigest() != expected:
            jar.unlink()
            raise RuntimeError("OpenAPI Generator checksum mismatch")
    java = os.getenv("JAVA", "java")
    subprocess.run([java, "-jar", str(jar), "generate", "-g", "python-fastapi",
                    "-i", str(enriched), "-o", str(build / "generated"),
                    "-t", str(ROOT / "templates"),
                    "--global-property", "apiTests=false,modelTests=false"], check=True)
    # Query values arrive as text; only JSON-body integers must use strict=True.
    for path in (build / "generated/src/openapi_server/apis").glob("*_api.py"):
        text = path.read_text("utf-8")
        text = text.replace("strict=True, ", "").replace(", strict=True", "")
        text = re.sub(r"id: StrictStr = Path", "id: UUID = Path", text)
        text = text.replace("Body(None,", "Body(...,")
        path.write_text(text, "utf-8")
    print("Generated handlers and DTOs: build/generated/src")


if __name__ == "__main__":
    main()
