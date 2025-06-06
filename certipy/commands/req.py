import argparse
import re
from typing import List

from impacket.dcerpc.v5 import rpcrt
from impacket.dcerpc.v5.dtypes import DWORD, LPWSTR, NULL, PBYTE, ULONG
from impacket.dcerpc.v5.ndr import NDRCALL, NDRSTRUCT
from impacket.dcerpc.v5.nrpc import checkNullString
from impacket.dcerpc.v5.rpcrt import RPC_C_AUTHN_LEVEL_PKT_PRIVACY
from impacket.dcerpc.v5.dcom.oaut import string_to_bin
from impacket.dcerpc.v5.dcomrt import DCOMCALL, DCOMANSWER
from impacket.uuid import uuidtup_to_bin
import httpx
from httpx_ntlm import HttpNtlmAuth

from certipy.lib.certificate import (
    cert_id_to_parts,
    cert_to_pem,
    create_csr,
    create_key_archival,
    create_on_behalf_of,
    create_pfx,
    create_renewal,
    csr_to_der,
    der_to_cert,
    der_to_csr,
    der_to_pem,
    get_identifications_from_certificate,
    get_object_sid_from_certificate,
    key_to_pem,
    load_pfx,
    pem_to_cert,
    pem_to_key,
    rsa,
    x509,
)
from certipy.lib.kerberos import HttpxImpacketKerberosAuth
from certipy.lib.errors import translate_error_code
from certipy.lib.formatting import print_certificate_identifications
from certipy.lib.logger import logging
from certipy.lib.rpc import get_dce_rpc, get_dcom_connection
from certipy.lib.target import Target
from certipy.commands.ca import ICertCustom
from certipy.lib.constants import OID_TO_STR_MAP

from .ca import CA

MSRPC_UUID_ICPR = uuidtup_to_bin(("91ae6020-9e3c-11cf-8d7c-00aa00c091be", "0.0"))

# https://winprotocoldoc.blob.core.windows.net/productionwindowsarchives/MS-WCCE/[MS-WCCE].pdf
CLSID_ICertRequest = string_to_bin('D99E6E74-FC88-11D0-B498-00A0C90312F3')
IID_ICertRequestD = uuidtup_to_bin(('D99E6E70-FC88-11D0-B498-00A0C90312F3', '0.0'))


class ICertRequestD(ICertCustom):
    '''
    ICertRequestD DCOM interface.
    '''

    def __init__(self, interface):
        super().__init__(interface)
        self._iid = IID_ICertRequestD


class DCERPCSessionError(rpcrt.DCERPCException):
    def __init__(self, error_string=None, error_code=None, packet=None):
        rpcrt.DCERPCException.__init__(self, error_string, error_code, packet)

    def __str__(self) -> str:
        self.error_code &= 0xFFFFFFFF
        error_msg = translate_error_code(self.error_code)
        return "RequestSessionError: %s" % error_msg


# https://docs.microsoft.com/en-us/openspecs/windows_protocols/ms-wcce/d6bee093-d862-4122-8f2b-7b49102097dc
class CERTTRANSBLOB(NDRSTRUCT):
    structure = (
        ("cb", ULONG),
        ("pb", PBYTE),
    )

# https://docs.microsoft.com/en-us/openspecs/windows_protocols/ms-icpr/0c6f150e-3ead-4006-b37f-ebbf9e2cf2e7
class CertServerRequest(NDRCALL):
    opnum = 0
    structure = (
        ("dwFlags", DWORD),
        ("pwszAuthority", LPWSTR),
        ("pdwRequestId", DWORD),
        ("pctbAttribs", CERTTRANSBLOB),
        ("pctbRequest", CERTTRANSBLOB),
    )


# https://winprotocoldoc.blob.core.windows.net/productionwindowsarchives/MS-WCCE/[MS-WCCE].pdf
class CertServerRequestD(DCOMCALL):
    opnum = 3
    structure = (
       ('dwFlags', DWORD),
       ('pwszAuthority', LPWSTR),
       ('pdwRequestId', DWORD),
       ('pwszAttributes', LPWSTR),
       ("pctbRequest", CERTTRANSBLOB),
    )


# https://docs.microsoft.com/en-us/openspecs/windows_protocols/ms-icpr/0c6f150e-3ead-4006-b37f-ebbf9e2cf2e7
class CertServerRequestResponse(NDRCALL):
    structure = (
        ("pdwRequestId", DWORD),
        ("pdwDisposition", ULONG),
        ("pctbCert", CERTTRANSBLOB),
        ("pctbEncodedCert", CERTTRANSBLOB),
        ("pctbDispositionMessage", CERTTRANSBLOB),
    )


# https://winprotocoldoc.blob.core.windows.net/productionwindowsarchives/MS-WCCE/[MS-WCCE].pdf
class CertServerRequestDResponse(DCOMANSWER):
    structure = (
        ('pdwRequestId', DWORD),
        ('pdwDisposition', ULONG),
        ('pctbCertChain', CERTTRANSBLOB),
        ('pctbEncodedCert', CERTTRANSBLOB),
        ('pctbDispositionMessage', CERTTRANSBLOB),
    )


class RequestInterface:
    def __init__(self, parent: "Request"):
        self.parent = parent

    def retrieve(self, request_id: int) -> x509.Certificate:
        raise NotImplementedError("Abstract method")

    def request(
        self,
        csr: bytes,
        attributes: List[str],
    ) -> x509.Certificate:
        raise NotImplementedError("Abstract method")


class DCOMRequestInterface(RequestInterface):
    '''
    Request interface for DCOM.
    '''

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dcom = None

    @property
    def dcom(self) -> rpcrt.DCERPC_v5:
        '''
        Establish a DCOM connection to the target using certipy's get_dcom_connection
        function.
        '''
        if self._dcom is not None:
            return self._dcom

        self._dcom = get_dcom_connection(self.parent.target)
        return self._dcom

    def retrieve(self, request_id: int) -> x509.Certificate:
        '''
        Retrieve an already requested certificate via request_id. Only the first
        few lines until the cert_req_d.request were modified. The rest is the
        same code as for the RPC interface.
        '''
        empty = CERTTRANSBLOB()
        empty['cb'] = 0
        empty['pb'] = NULL

        request = CertServerRequestD()
        request['dwFlags'] = 0
        request['pwszAuthority'] = checkNullString(self.parent.ca)
        request['pdwRequestId'] = request_id
        request['pwszAttributes'] = empty
        request['pctbRequest'] = empty

        logging.info(f'Rerieving certificate with ID {request_id}')

        i_cert_req = self.dcom.CoCreateInstanceEx(CLSID_ICertRequest, IID_ICertRequestD)
        i_cert_req.get_cinstance().set_auth_level(RPC_C_AUTHN_LEVEL_PKT_PRIVACY)

        cert_req_d = ICertRequestD(i_cert_req)
        response = cert_req_d.request(request)

        error_code = response["pdwDisposition"]

        if error_code == 3:
            logging.info("Successfully retrieved certificate")
        else:
            if error_code == 5:
                logging.warning("Certificate request is still pending approval")
            else:
                error_msg = translate_error_code(error_code)
                if "unknown error code" in error_msg:
                    logging.error(
                        "Got unknown error while trying to retrieve certificate: (%s): %s"
                        % (
                            error_msg,
                            b"".join(response["pctbDispositionMessage"]["pb"]).decode(
                                "utf-16le"
                            ),
                        )
                    )
                else:
                    logging.error(
                        "Got error while trying to retrieve certificate: %s" % error_msg
                    )

            return False

        cert = der_to_cert(b"".join(response["pctbEncodedCert"]["pb"]))

        return cert

    def request(self, csr: bytes, attributes: List[str]) -> x509.Certificate:
        '''
        Request a new certificate via CSR. Only the first few lines until the
        cert_req_d.request were modified. The rest is the same code as for the
        RPC interface.
        '''
        attributes = checkNullString("\n".join(attributes))

        pctb_request = CERTTRANSBLOB()
        pctb_request["cb"] = len(csr)
        pctb_request["pb"] = csr

        request = CertServerRequestD()
        request["dwFlags"] = 0
        request["pwszAuthority"] = checkNullString(self.parent.ca)
        request["pdwRequestId"] = self.parent.request_id
        request["pwszAttributes"] = attributes
        request["pctbRequest"] = pctb_request

        logging.info("Requesting certificate via DCOM")

        i_cert_req = self.dcom.CoCreateInstanceEx(CLSID_ICertRequest, IID_ICertRequestD)
        i_cert_req.get_cinstance().set_auth_level(RPC_C_AUTHN_LEVEL_PKT_PRIVACY)

        cert_req_d = ICertRequestD(i_cert_req)
        response = cert_req_d.request(request)

        error_code = response["pdwDisposition"]
        request_id = response["pdwRequestId"]

        if error_code == 3:
            logging.info("Successfully requested certificate")

        else:
            if error_code == 5:
                logging.warning("Certificate request is pending approval")
            else:
                error_msg = translate_error_code(error_code)
                if "unknown error code" in error_msg:
                    logging.error(
                        "Got unknown error while trying to request certificate: (%s): %s"
                        % (
                            error_msg,
                            b"".join(response["pctbDispositionMessage"]["pb"]).decode(
                                "utf-16le"
                            ),
                        )
                    )
                else:
                    logging.error(
                        "Got error while trying to request certificate: %s" % error_msg
                    )

        logging.info("Request ID is %d" % request_id)

        if error_code != 3:
            should_save = input(
                "Would you like to save the private key? (y/N) "
            ).rstrip("\n")

            if should_save.lower() == "y":
                out = (
                    self.parent.out if self.parent.out is not None else str(request_id)
                )
                with open("%s.key" % out, "wb") as f:
                    f.write(key_to_pem(self.parent.key))

                logging.info("Saved private key to %s.key" % out)

            return False

        cert = der_to_cert(b"".join(response["pctbEncodedCert"]["pb"]))

        return cert


class RPCRequestInterface(RequestInterface):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self._dce = None

    @property
    def dce(self) -> rpcrt.DCERPC_v5:
        if self._dce is not None:
            return self._dce

        self._dce = get_dce_rpc(
            MSRPC_UUID_ICPR,
            r"\pipe\cert",
            self.parent.target,
            timeout=self.parent.target.timeout,
            dynamic=self.parent.dynamic,
            verbose=self.parent.verbose,
        )

        return self._dce

    def retrieve(self, request_id: int) -> x509.Certificate:

        empty = CERTTRANSBLOB()
        empty["cb"] = 0
        empty["pb"] = NULL

        request = CertServerRequest()
        request["dwFlags"] = 0
        request["pwszAuthority"] = checkNullString(self.parent.ca)
        request["pdwRequestId"] = request_id
        request["pctbAttribs"] = empty
        request["pctbRequest"] = empty

        logging.info("Retrieving certificate with ID %d" % request_id)

        response = self.dce.request(request, checkError=False)

        error_code = response["pdwDisposition"]

        if error_code == 3:
            logging.info("Successfully retrieved certificate")
        else:
            if error_code == 5:
                logging.warning("Certificate request is still pending approval")
            else:
                error_msg = translate_error_code(error_code)
                if "unknown error code" in error_msg:
                    logging.error(
                        "Got unknown error while trying to retrieve certificate: (%s): %s"
                        % (
                            error_msg,
                            b"".join(response["pctbDispositionMessage"]["pb"]).decode(
                                "utf-16le"
                            ),
                        )
                    )
                else:
                    logging.error(
                        "Got error while trying to retrieve certificate: %s" % error_msg
                    )

            return False

        cert = der_to_cert(b"".join(response["pctbEncodedCert"]["pb"]))

        return cert

    def request(
        self,
        csr: bytes,
        attributes: List[str],
    ) -> x509.Certificate:
        attributes = checkNullString("\n".join(attributes)).encode("utf-16le")
        pctb_attribs = CERTTRANSBLOB()
        pctb_attribs["cb"] = len(attributes)
        pctb_attribs["pb"] = attributes

        pctb_request = CERTTRANSBLOB()
        pctb_request["cb"] = len(csr)
        pctb_request["pb"] = csr

        request = CertServerRequest()
        request["dwFlags"] = 0
        request["pwszAuthority"] = checkNullString(self.parent.ca)
        request["pdwRequestId"] = self.parent.request_id
        request["pctbAttribs"] = pctb_attribs
        request["pctbRequest"] = pctb_request

        logging.info("Requesting certificate via RPC")

        response = self.dce.request(request)

        error_code = response["pdwDisposition"]
        request_id = response["pdwRequestId"]

        if error_code == 3:
            logging.info("Successfully requested certificate")
        else:
            if error_code == 5:
                logging.warning("Certificate request is pending approval")
            else:
                error_msg = translate_error_code(error_code)
                if "unknown error code" in error_msg:
                    logging.error(
                        "Got unknown error while trying to request certificate: (%s): %s"
                        % (
                            error_msg,
                            b"".join(response["pctbDispositionMessage"]["pb"]).decode(
                                "utf-16le"
                            ),
                        )
                    )
                else:
                    logging.error(
                        "Got error while trying to request certificate: %s" % error_msg
                    )

        logging.info("Request ID is %d" % request_id)

        if error_code != 3:
            should_save = input(
                "Would you like to save the private key? (y/N) "
            ).rstrip("\n")

            if should_save.lower() == "y":
                out = (
                    self.parent.out if self.parent.out is not None else str(request_id)
                )
                with open("%s.key" % out, "wb") as f:
                    f.write(key_to_pem(self.parent.key))

                logging.info("Saved private key to %s.key" % out)

            return False

        cert = der_to_cert(b"".join(response["pctbEncodedCert"]["pb"]))

        return cert


class WebRequestInterface(RequestInterface):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.target = self.parent.target

        self._session = None
        self.base_url = ""

    @property
    def session(self) -> httpx.Client:
        if self._session is not None:
            return self._session

        # Create a session with httpx
        if self.target.do_kerberos:
            session = httpx.Client(auth=HttpxImpacketKerberosAuth(self.target), timeout=self.target.timeout, verify=False)
        else:
            password = self.target.password
            if self.target.nthash:
                password = "%s:%s" % (self.target.nthash, self.target.nthash)

            principal = "%s\\%s" % (self.target.domain, self.target.username)
            session = httpx.Client(auth=HttpNtlmAuth(principal, password), timeout=self.target.timeout, verify=False)
            
        scheme = self.parent.scheme
        port = self.parent.port
        base_url = "%s://%s:%i" % (scheme, self.target.target_ip, port)
        logging.info("Checking for Web Enrollment on %s" % repr(base_url))

        success = False
        try:
            res = session.get(
                "%s/certsrv/" % base_url,
                headers={"Host": self.target.remote_name},
                timeout=self.target.timeout,
                follow_redirects=False,
            )
        except Exception as e:
            logging.warning("Failed to connect to Web Enrollment interface: %s" % e)
        else:
            if res.status_code == 200:
                success = True
            elif res.status_code == 401:
                logging.error("Unauthorized for Web Enrollment at %s" % repr(base_url))
                return None
            else:
                logging.warning(
                    "Failed to authenticate to Web Enrollment at %s" % repr(base_url)
                )
                logging.debug(
                    "Got status code: %s" % repr(res.status_code)
                )
                logging.debug(
                    "HTML Response:\n%s" % repr(res.content)
                )

        if not success:
            scheme = "https" if scheme == "http" else "http"
            port = 80 if scheme == "http" else 443
            base_url = "%s://%s:%i" % (scheme, self.target.target_ip, port)
            logging.info(
                "Trying to connect to Web Enrollment interface %s" % repr(base_url)
            )

            try:
                res = session.get(
                    "%s/certsrv/" % base_url,
                    headers={"Host": self.target.remote_name},
                    timeout=self.target.timeout,
                    follow_redirects=False,
                )
            except Exception as e:
                logging.warning("Failed to connect to Web Enrollment interface: %s" % e)
                return None
            else:
                if res.status_code == 200:
                    success = True
                elif res.status_code == 401:
                    logging.error(
                        "Unauthorized for Web Enrollment at %s" % repr(base_url)
                    )
                else:
                    logging.warning(
                        "Failed to authenticate to Web Enrollment at %s"
                        % repr(base_url)
                    )
                    logging.debug(
                        "Got status code: %s" % repr(res.status_code)
                    )
                    logging.debug(
                        "HTML Response:\n%s" % repr(res.content)
                    )

        if not success:
            return None

        self.base_url = base_url
        self._session = session
        return self._session

    def retrieve(self, request_id: int) -> x509.Certificate:
        logging.info("Retrieving certificate for request ID: %d" % request_id)
        res = self.session.get(
            "%s/certsrv/certnew.cer" % self.base_url, params={"ReqID": request_id}
        )

        if res.status_code != 200:
            if self.parent.verbose:
                logging.error("Got error while trying to retrieve certificate:")
                print(res.text)
            else:
                logging.error(
                    "Got error while trying to retrieve certificate. Use -debug to print the response"
                )
            return False

        if b"BEGIN CERTIFICATE" in res.content:
            cert = pem_to_cert(res.content)
        else:
            content = res.text
            if "Taken Under Submission" in content:
                logging.warning("Certificate request is pending approval")
            elif "The requested property value is empty" in content:
                logging.warning("Unknown request ID %d" % request_id)
            else:
                error_code = re.findall(r" (0x[0-9a-fA-F]+) \(", content)
                try:
                    error_code = int(error_code[0], 16)
                    msg = translate_error_code(error_code)
                    logging.warning("Got error from AD CS: %s" % msg)
                except:
                    if self.parent.verbose:
                        logging.warning("Got unknown error from AD CS:")
                        print(content)
                    else:
                        logging.warning(
                            "Got unknown error from AD CS. Use -debug to print the response"
                        )

            return False

        return cert

    def request(
        self,
        csr: bytes,
        attributes: List[str],
    ) -> x509.Certificate:
        session = self.session
        if not session:
            return False

        csr = der_to_pem(csr, "CERTIFICATE REQUEST")

        attributes = "\n".join(attributes)

        params = {
            "Mode": "newreq",
            "CertAttrib": attributes,
            "CertRequest": csr,
            "TargetStoreFlags": "0",
            "SaveCert": "yes",
            "ThumbPrint": "",
        }

        logging.info("Requesting certificate via Web Enrollment")

        res = session.post("%s/certsrv/certfnsh.asp" % self.base_url, data=params)
        content = res.text

        if res.status_code != 200:
            logging.error("Got error while trying to request certificate: ")
            if self.parent.verbose:
                print(content)
            else:
                logging.warning("Use -debug to print the response")
            return False

        request_id = re.findall(r"certnew.cer\?ReqID=([0-9]+)&", content)
        if not request_id:
            if "template that is not supported" in content:
                logging.error(
                    "Template %s is not supported by AD CS" % repr(self.parent.template)
                )
                return False
            else:
                request_id = re.findall(r"Your Request Id is ([0-9]+)", content)
                if len(request_id) != 1:
                    logging.error("Failed to get request id from response")
                    request_id = None
                else:
                    request_id = int(request_id[0])

                    logging.info("Request ID is %d" % request_id)

                if "Certificate Pending" in content:
                    logging.warning("Certificate request is pending approval")
                elif '"Denied by Policy Module"' in content:
                    res = self.session.get(
                        "%s/certsrv/certnew.cer" % self.base_url,
                        params={"ReqID": request_id},
                    )
                    try:
                        error_codes = re.findall(
                            "(0x[a-zA-Z0-9]+) \([-]?[0-9]+ ",
                            res.text,
                            flags=re.MULTILINE,
                        )

                        error_msg = translate_error_code(int(error_codes[0], 16))
                        logging.error(
                            "Got error while trying to request certificate: %s"
                            % error_msg
                        )
                    except:
                        logging.warning("Got unknown error from AD CS:")
                        if self.parent.verbose:
                            print(res.text)
                        else:
                            logging.warning("Use -debug to print the response")
                else:
                    error_code = re.findall(
                        r"Denied by Policy Module  (0x[0-9a-fA-F]+),", content
                    )
                    try:
                        error_code = int(error_code[0], 16)
                        msg = translate_error_code(error_code)
                        logging.warning("Got error from AD CS: %s" % msg)
                    except:
                        logging.warning("Got unknown error from AD CS:")
                        if self.parent.verbose:
                            print(content)
                        else:
                            logging.warning("Use -debug to print the response")

            if request_id is None:
                return False

            should_save = input(
                "Would you like to save the private key? (y/N) "
            ).rstrip("\n")

            if should_save.lower() == "y":
                out = (
                    self.parent.out if self.parent.out is not None else str(request_id)
                )
                with open("%s.key" % out, "wb") as f:
                    f.write(key_to_pem(self.parent.key))

                logging.info("Saved private key to %s.key" % out)

            return False

        if len(request_id) == 0:
            logging.error("Failed to get request id from response")
            return False

        request_id = int(request_id[0])

        logging.info("Request ID is %d" % request_id)

        return self.retrieve(request_id)

class Request:
    def __init__(
        self,
        target: Target = None,
        ca: str = None,
        template: str = None,
        upn: str = None,
        dns: str = None,
        sid: str = None,
        subject: str = None,
        retrieve: int = 0,
        on_behalf_of: str = None,
        pfx: str = None,
        key_size: int = None,
        archive_key: bool = False,
        renew: bool = False,
        out: str = None,
        key: rsa.RSAPrivateKey = None,
        web: bool = False,
        dcom: bool = False,
        port: int = None,
        scheme: str = None,
        dynamic_endpoint: bool = False,
        debug=False,
        application_policies: List[str] = None,
        **kwargs
    ):
        self.target = target
        self.ca = ca
        self.template = template
        self.alt_upn = upn
        self.alt_dns = dns
        self.alt_sid = sid
        self.subject = subject
        self.request_id = int(retrieve)
        self.on_behalf_of = on_behalf_of
        self.pfx = pfx
        self.key_size = key_size
        self.archive_key = archive_key
        self.renew = renew
        self.out = out
        self.key = key
        self.application_policies = [
            OID_TO_STR_MAP.get(policy, policy) for policy in (application_policies or [])
        ]

        self.web = web
        self.dcom = dcom
        self.port = port
        self.scheme = scheme

        self.dynamic = dynamic_endpoint
        self.verbose = debug
        self.kwargs = kwargs

        if not self.port and self.scheme:
            if self.scheme == "http":
                self.port = 80
            elif self.scheme == "https":
                self.port = 443

        self._dce = None

        self._interface = None

    @property
    def interface(self) -> RequestInterface:
        if self._interface is not None:
            return self._interface

        if self.web:
            self._interface = WebRequestInterface(self)

        elif self.dcom:
            self._interface = DCOMRequestInterface(self)

        else:
            self._interface = RPCRequestInterface(self)

        return self._interface

    def retrieve(self) -> bool:
        request_id = int(self.request_id)

        cert = self.interface.retrieve(request_id)
        if cert is False:
            logging.error("Failed to retrieve certificate")
            return False

        identifications = get_identifications_from_certificate(cert)

        print_certificate_identifications(identifications)

        object_sid = get_object_sid_from_certificate(cert)
        if object_sid is not None:
            logging.info("Certificate object SID is %s" % repr(object_sid))
        else:
            logging.info("Certificate has no object SID")

        out = self.out
        if out is None:
            out, _ = cert_id_to_parts(identifications)
            if out is None:
                out = self.target.username

            out = out.rstrip("$").lower()

        try:
            with open("%d.key" % request_id, "rb") as f:
                key = pem_to_key(f.read())
        except Exception as e:
            logging.warning(
                "Could not find matching private key. Saving certificate as PEM"
            )
            with open("%s.crt" % out, "wb") as f:
                f.write(cert_to_pem(cert))

            logging.info("Saved certificate to %s" % repr("%s.crt" % out))
        else:
            logging.info("Loaded private key from %s" % repr("%d.key" % request_id))
            pfx = create_pfx(key, cert)
            with open("%s.pfx" % out, "wb") as f:
                f.write(pfx)
            logging.info(
                "Saved certificate and private key to %s" % repr("%s.pfx" % out)
            )

        return True

    def request(self) -> bool:
        username = self.target.username

        if sum(map(bool, [self.archive_key, self.on_behalf_of, self.renew])) > 1:
            logging.error(
                "Combinations of -renew, -on-behalf-of, and -archive-key are currently not supported"
            )
            return None

        if self.on_behalf_of:
            username = self.on_behalf_of
            if self.on_behalf_of.count("\\") > 0:
                parts = username.split("\\")
                username = "\\".join(parts[1:])
                domain = parts[0]
                if "." in domain:
                    logging.warning(
                        "Domain part of '-on-behalf-of' should not be a FQDN"
                    )

        renewal_cert = None
        renewal_key = None
        if self.renew:
            if self.pfx is None:
                logging.error(
                    "A certificate and private key (-pfx) is required in order for renewal"
                )
                return False

            with open(self.pfx, "rb") as f:
                renewal_key, renewal_cert = load_pfx(f.read())

        converted_policies = []
        for policy in self.application_policies:
            oid = next((k for k, v in OID_TO_STR_MAP.items() if v.lower() == policy.lower()), policy)
            converted_policies.append(oid)
        
        self.application_policies = converted_policies

        csr, key = create_csr(
            username,
            alt_dns=self.alt_dns,
            alt_upn=self.alt_upn,
            alt_sid=self.alt_sid,
            key=self.key,
            key_size=self.key_size,
            subject=self.subject,
            renewal_cert=renewal_cert,
            application_policies=self.application_policies
        )
        self.key = key

        csr = csr_to_der(csr)

        if self.archive_key:
            ca = CA(self.target, self.ca)
            logging.info("Trying to retrieve CAX certificate")
            cax_cert = ca.get_exchange_certificate()
            logging.info("Retrieved CAX certificate")

            csr = create_key_archival(der_to_csr(csr), self.key, cax_cert)

        if self.renew:
            csr = create_renewal(csr, renewal_cert, renewal_key)

        if self.on_behalf_of:
            if self.pfx is None:
                logging.error(
                    "A certificate and private key (-pfx) is required in order to request on behalf of another user"
                )
                return False

            with open(self.pfx, "rb") as f:
                agent_key, agent_cert = load_pfx(f.read())

            csr = create_on_behalf_of(csr, self.on_behalf_of, agent_cert, agent_key)

        # Construct attributes list
        attributes = ["CertificateTemplate:%s" % self.template]

        if self.alt_upn is not None or self.alt_dns is not None:
            san = []
            if self.alt_dns:
                san.append("dns=%s" % self.alt_dns)
            if self.alt_upn:
                san.append("upn=%s" % self.alt_upn)

            attributes.append("SAN:%s" % "&".join(san))

        if self.application_policies:
            policy_string = "&".join(self.application_policies)
            attributes.append(f"ApplicationPolicies:{policy_string}")

        cert = self.interface.request(csr, attributes)

        if cert is False:
            logging.error("Failed to request certificate")
            return False

        if self.subject:
            subject = ",".join(map(lambda x: x.rfc4514_string(), cert.subject.rdns))
            logging.info("Got certificate with subject: %s" % subject)

        identifications = get_identifications_from_certificate(cert)

        print_certificate_identifications(identifications)

        object_sid = get_object_sid_from_certificate(cert)
        if object_sid is not None:
            logging.info("Certificate object SID is %s" % repr(object_sid))
        else:
            logging.info("Certificate has no object SID")

        out = self.out
        if out is None:
            out, _ = cert_id_to_parts(identifications)
            if out is None:
                out = self.target.username

            out = out.rstrip("$").lower()

        pfx = create_pfx(key, cert)

        outfile = "%s.pfx" % out

        with open(outfile, "wb") as f:
            f.write(pfx)

        logging.info("Saved certificate and private key to %s" % repr(outfile))

        return pfx, outfile


def entry(options: argparse.Namespace) -> None:
    target = Target.from_options(options)
    del options.target

    request = Request(target=target, **vars(options))

    if options.retrieve:
        request.retrieve()
    else:
        request.request()
