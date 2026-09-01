"""Automated local TLS certificate generator with SAN extensions for HTTPS/WSS."""

from pathlib import Path
import datetime
import ipaddress
from cryptography import x509
from cryptography.x509.oid import NameOID
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization


def generate_local_tls_certs(cert_dir: Path = Path("./certs")):
    cert_dir.mkdir(parents=True, exist_ok=True)
    key_path = cert_dir / "privkey.pem"
    cert_path = cert_dir / "fullchain.pem"

    if key_path.exists() and cert_path.exists():
        print("TLS certificates already exist. Skipping generation.")
        return

    print("Generating 2048-bit RSA Private Key...")
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048
    )

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Edge CCTV AI System"),
        x509.NameAttribute(NameOID.COMMON_NAME, "edge-cctv.local"),
    ])

    alt_names = [
        x509.DNSName("localhost"),
        x509.DNSName("edge-cctv.local"),
        x509.DNSName("*.local"),
        x509.DNSName("*.ts.net"),
        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
        x509.IPAddress(ipaddress.IPv4Address("192.168.1.100")),
    ]

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.utcnow())
        .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(alt_names), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(private_key, hashes.SHA256())
    )

    with open(key_path, "wb") as f:
        f.write(private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption()
        ))

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    print(f"Generated valid TLS certificates at {cert_dir.resolve()}")


if __name__ == "__main__":
    generate_local_tls_certs()
