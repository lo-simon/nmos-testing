# Copyright (C) 2019 Advanced Media Workflow Association
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from Crypto.PublicKey import RSA
from authlib.jose import jwt, JsonWebKey

import time
import uuid

from .NMOSUtils import NMOSUtils
from OpenSSL import crypto
from cryptography.hazmat.primitives import serialization
from cryptography import x509

from . import Config as CONFIG


class IS10Utils(NMOSUtils):
    def __init__(self, url):
        NMOSUtils.__init__(self, url)

    @staticmethod
    def read_RSA_private_key(private_key_files):
        """Load the 1st RSA private key from the given private key files"""
        for private_key_file in private_key_files:
            private_key = open(private_key_file, "r").read()
            if private_key.find("BEGIN RSA PRIVATE KEY") != -1:
                return private_key
        return None

    @staticmethod
    def generate_jwk(rsa_private_key):
        """Generate the JWK for a given RSA private key"""
        rsa_key = RSA.importKey(rsa_private_key)
        public_key = rsa_key.publickey().exportKey(format="PEM")
        return JsonWebKey.import_key(public_key, {"kty": "RSA", "use": "sig",
                                                  "key_ops": "verify", "alg": "RS512"}).as_dict()

    @staticmethod
    def generate_token(rsa_private_key, scopes=None, write=False, azp=False, add_claims=True, overrides=None):
        """Generate the access token with the given parameters"""
        if scopes is None:
            scopes = []
        header = {"typ": "JWT", "alg": "RS512"}
        payload = {"iss": "{}".format("https://testsuite.nmos.tv"),
                   "sub": "testsuite@nmos.tv",
                   "aud": ["https://*.{}".format(CONFIG.DNS_DOMAIN), "https://*.local"],
                   "exp": int(time.time() + 3600),
                   "iat": int(time.time()),
                   "scope": " ".join(scopes)}
        if azp:
            payload["azp"] = str(uuid.uuid4())
        else:
            payload["client_id"] = str(uuid.uuid4())
        nmos_claims = {}
        if add_claims:
            for api in scopes:
                nmos_claims["x-nmos-{}".format(api)] = {"read": ["*"]}
                if write:
                    nmos_claims["x-nmos-{}".format(api)]["write"] = ["*"]
        payload.update(nmos_claims)
        if overrides:
            payload.update(overrides)
        token = jwt.encode(header, payload, rsa_private_key).decode()
        return token

    @staticmethod
    def make_key_cert_files(cert_file, key_file):
        # create a key pair
        k = crypto.PKey()
        k.generate_key(crypto.TYPE_RSA, 2048)

        # create cert
        cert = crypto.X509()
        cert.set_version(2)
        cert.get_subject().C = "GB"
        cert.get_subject().ST = "England"
        cert.get_subject().O = "NMOS Testing Ltd"  # noqa: E741
        ca_cert_subject = cert.get_subject()
        ca_cert_subject.CN = "ca.testsuite.nmos.tv"
        cert.set_issuer(ca_cert_subject)
        cert.get_subject().CN = "mocks.testsuite.nmos.tv"
        cert.set_serial_number(x509.random_serial_number())
        cert.gmtime_adj_notBefore(0)
        cert.gmtime_adj_notAfter(10*365*24*60*60)
        cert.set_pubkey(k)
        # get Root CA key
        capkey = open(CONFIG.KEY_TRUST_ROOT_CA, "r").read()
        ca_pkey = crypto.load_privatekey(crypto.FILETYPE_PEM, capkey)
        # get Root CA cert
        cacert = open(CONFIG.CERT_TRUST_ROOT_CA, "r").read()
        ca_cert = crypto.load_certificate(crypto.FILETYPE_PEM, cacert)
        # create cert extension
        san = ["DNS:mocks.{}".format(CONFIG.DNS_DOMAIN), "DNS: nmos-mocks.local"]
        cert_ext = []
        cert_ext.append(crypto.X509Extension(b'subjectKeyIdentifier', False, b'hash', cert))
        cert_ext.append(crypto.X509Extension(b'authorityKeyIdentifier',
                                             False, b'keyid,issuer:always', issuer=ca_cert))
        cert_ext.append(crypto.X509Extension(b'basicConstraints', False, b'CA:FALSE'))
        cert_ext.append(crypto.X509Extension(b'keyUsage', True, b'digitalSignature, keyEncipherment'))
        cert_ext.append(crypto.X509Extension(b'subjectAltName', False, ','.join(san).encode()))
        cert.add_extensions(cert_ext)

        # sign cert with Intermediate CA key
        cert.sign(ca_pkey, 'sha256')

        # write chain certificate file
        if cert_file is not None:
            with open(cert_file, "wt") as f:
                f.write(crypto.dump_certificate(crypto.FILETYPE_PEM, cert).decode("utf-8"))
                f.write(cacert)
        # write private key file
        if key_file is not None:
            with open(key_file, "wb") as f:
                pem = k.to_cryptography_key().private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption()
                )
                f.write(pem)