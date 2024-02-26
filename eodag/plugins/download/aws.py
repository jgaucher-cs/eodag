# -*- coding: utf-8 -*-
# Copyright 2018, CS GROUP - France, https://www.csgroup.eu/
#
# This file is part of EODAG project
#     https://www.github.com/CS-SI/EODAG
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
from __future__ import annotations

import logging
import os
import re
from datetime import datetime
from itertools import chain
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Match,
    Optional,
    Set,
    Tuple,
    Union,
    cast,
)

import boto3
import requests
from botocore.exceptions import ClientError, ProfileNotFound
from botocore.handlers import disable_signing
from lxml import etree
from stream_zip import ZIP_AUTO, stream_zip

from eodag.api.product.metadata_mapping import (
    mtd_cfg_as_conversion_and_querypath,
    properties_from_json,
    properties_from_xml,
)
from eodag.plugins.download.base import Download
from eodag.utils import (
    DEFAULT_DOWNLOAD_TIMEOUT,
    DEFAULT_DOWNLOAD_WAIT,
    HTTP_REQ_TIMEOUT,
    USER_AGENT,
    ProgressCallback,
    flatten_top_directories,
    get_bucket_name_and_prefix,
    path_to_uri,
    rename_subfolder,
    sanitize,
)
from eodag.utils.exceptions import (
    AuthenticationError,
    DownloadError,
    NotAvailableError,
    TimeOutError,
)

if TYPE_CHECKING:
    from boto3.resources.collection import ResourceCollection

    from eodag.api.product import EOProduct
    from eodag.api.search_result import SearchResult
    from eodag.config import PluginConfig
    from eodag.utils import DownloadedCallback


logger = logging.getLogger("eodag.download.aws")

# AWS chunk path identify patterns

# S2 L2A Tile files -----------------------------------------------------------
S2L2A_TILE_IMG_REGEX = re.compile(
    r"^tiles/(?P<tile1>[0-9]+)/(?P<tile2>[A-Z]+)/(?P<tile3>[A-Z]+)/(?P<year>[0-9]+)/(?P<month>[0-9]+)/"
    + r"(?P<day>[0-9]+)/(?P<num>[0-9]+)/R(?P<res>[0-9]+m)/(?P<file>[A-Z0-9_]+)\.jp2$"
)
S2L2A_TILE_AUX_DIR_REGEX = re.compile(
    r"^tiles/(?P<tile1>[0-9]+)/(?P<tile2>[A-Z]+)/(?P<tile3>[A-Z]+)/(?P<year>[0-9]+)/(?P<month>[0-9]+)/"
    + r"(?P<day>[0-9]+)/(?P<num>[0-9]+)/auxiliary/(?P<file>AUX_.+)$"
)
# S2 L2A QI Masks
S2_TILE_QI_MSK_REGEX = re.compile(
    r"^tiles/(?P<tile1>[0-9]+)/(?P<tile2>[A-Z]+)/(?P<tile3>[A-Z]+)/(?P<year>[0-9]+)/(?P<month>[0-9]+)/"
    + r"(?P<day>[0-9]+)/(?P<num>[0-9]+)/qi/(?P<file_base>.+)_(?P<file_suffix>[0-9]+m\.jp2)$"
)
# S2 L2A QI PVI
S2_TILE_QI_PVI_REGEX = re.compile(
    r"^tiles/(?P<tile1>[0-9]+)/(?P<tile2>[A-Z]+)/(?P<tile3>[A-Z]+)/(?P<year>[0-9]+)/(?P<month>[0-9]+)/"
    + r"(?P<day>[0-9]+)/(?P<num>[0-9]+)/qi/L2A_PVI\.jp2$"
)
# S2 Tile files ---------------------------------------------------------------
S2_TILE_IMG_REGEX = re.compile(
    r"^tiles/(?P<tile1>[0-9]+)/(?P<tile2>[A-Z]+)/(?P<tile3>[A-Z]+)/(?P<year>[0-9]+)/(?P<month>[0-9]+)/"
    + r"(?P<day>[0-9]+)/(?P<num>[0-9]+)/(?P<file>[A-Z0-9_]+\.jp2)$"
)
S2_TILE_PREVIEW_DIR_REGEX = re.compile(
    r"^tiles/(?P<tile1>[0-9]+)/(?P<tile2>[A-Z]+)/(?P<tile3>[A-Z]+)/(?P<year>[0-9]+)/(?P<month>[0-9]+)/"
    + r"(?P<day>[0-9]+)/(?P<num>[0-9]+)/preview/(?P<file>.+)$"
)
S2_TILE_AUX_DIR_REGEX = re.compile(
    r"^tiles/(?P<tile1>[0-9]+)/(?P<tile2>[A-Z]+)/(?P<tile3>[A-Z]+)/(?P<year>[0-9]+)/(?P<month>[0-9]+)/"
    + r"(?P<day>[0-9]+)/(?P<num>[0-9]+)/auxiliary/(?P<file>.+)$"
)
S2_TILE_QI_DIR_REGEX = re.compile(
    r"^tiles/(?P<tile1>[0-9]+)/(?P<tile2>[A-Z]+)/(?P<tile3>[A-Z]+)/(?P<year>[0-9]+)/(?P<month>[0-9]+)/"
    + r"(?P<day>[0-9]+)/(?P<num>[0-9]+)/qi/(?P<file>.+)$"
)
S2_TILE_THUMBNAIL_REGEX = re.compile(
    r"^tiles/(?P<tile1>[0-9]+)/(?P<tile2>[A-Z]+)/(?P<tile3>[A-Z]+)/(?P<year>[0-9]+)/(?P<month>[0-9]+)/"
    + r"(?P<day>[0-9]+)/(?P<num>[0-9]+)/(?P<file>preview\.\w+)$"
)
S2_TILE_MTD_REGEX = re.compile(
    r"^tiles/(?P<tile1>[0-9]+)/(?P<tile2>[A-Z]+)/(?P<tile3>[A-Z]+)/(?P<year>[0-9]+)/(?P<month>[0-9]+)/"
    + r"(?P<day>[0-9]+)/(?P<num>[0-9]+)/(?P<file>metadata\.xml)$"
)
# S2 Tile generic
S2_TILE_REGEX = re.compile(
    r"^tiles/(?P<tile1>[0-9]+)/(?P<tile2>[A-Z]+)/(?P<tile3>[A-Z]+)/(?P<year>[0-9]+)/(?P<month>[0-9]+)/"
    + r"(?P<day>[0-9]+)/(?P<num>[0-9]+)/(?P<file>.+)$"
)
# S2 Product files
S2_PROD_REGEX = re.compile(
    r"^products/(?P<year>[0-9]+)/(?P<month>[0-9]+)/(?P<day>[0-9]+)/(?P<title>[A-Z0-9_]+)/(?P<file>.+)$"
)
S2_PROD_DS_MTD_REGEX = re.compile(
    r"^products/(?P<year>[0-9]+)/(?P<month>[0-9]+)/(?P<day>[0-9]+)/(?P<title>[A-Z0-9_]+)/datastrip/"
    + r"(?P<num>.+)/(?P<file>metadata\.xml)$"
)
S2_PROD_DS_QI_REGEX = re.compile(
    r"^products/(?P<year>[0-9]+)/(?P<month>[0-9]+)/(?P<day>[0-9]+)/(?P<title>[A-Z0-9_]+)/datastrip/"
    + r"(?P<num>.+)/qi/(?P<file>.+)$"
)
S2_PROD_DS_QI_REPORT_REGEX = re.compile(
    r"^products/(?P<year>[0-9]+)/(?P<month>[0-9]+)/(?P<day>[0-9]+)/(?P<title>[A-Z0-9_]+)/datastrip/"
    + r"(?P<num>.+)/qi/(?P<filename>.+)_report\.xml$"
)
S2_PROD_INSPIRE_REGEX = re.compile(
    r"^products/(?P<year>[0-9]+)/(?P<month>[0-9]+)/(?P<day>[0-9]+)/(?P<title>[A-Z0-9_]+)/"
    + r"(?P<file>inspire\.xml)$"
)
# S2 Product generic
S2_PROD_MTD_REGEX = re.compile(
    r"^products/(?P<year>[0-9]+)/(?P<month>[0-9]+)/(?P<day>[0-9]+)/(?P<title>[A-Z0-9_]+)/"
    + r"(?P<file>metadata\.xml)$"
)
# S1 files --------------------------------------------------------------------
S1_CALIB_REGEX = re.compile(
    r"^GRD/(?P<year>[0-9]+)/(?P<month>[0-9]+)/(?P<day>[0-9]+)/(?P<beam>[A-Z0-9]+)/(?P<prod_pol>[A-Z0-9]+)/"
    + r"(?P<title>[A-Z0-9_]+)/annotation/calibration/"
    + r"(?P<file_prefix>[a-z]+)-(?P<file_beam>[a-z]+)-(?P<file_pol>.+)\.xml$"
)
S1_ANNOT_REGEX = re.compile(
    r"^GRD/(?P<year>[0-9]+)/(?P<month>[0-9]+)/(?P<day>[0-9]+)/(?P<beam>[A-Z0-9]+)/(?P<prod_pol>[A-Z0-9]+)/"
    + r"(?P<title>[A-Z0-9_]+)/annotation/"
    + r"(?P<file_beam>[a-z]+)-(?P<file_pol>.+)\.xml$"
)
S1_MEAS_REGEX = re.compile(
    r"^GRD/(?P<year>[0-9]+)/(?P<month>[0-9]+)/(?P<day>[0-9]+)/(?P<beam>[A-Z0-9]+)/(?P<prod_pol>[A-Z0-9]+)/"
    + r"(?P<title>[A-Z0-9_]+)/measurement/"
    + r"(?P<file_beam>[a-z]+)-(?P<file_pol>.+)\.(?P<file_ext>[a-z0-9]+)$"
)
S1_REPORT_REGEX = re.compile(
    r"^GRD/(?P<year>[0-9]+)/(?P<month>[0-9]+)/(?P<day>[0-9]+)/(?P<beam>[A-Z0-9]+)/(?P<prod_pol>[A-Z0-9]+)/"
    + r"(?P<title>[A-Z0-9_]+)/(?P<file>report-\w+\.pdf)$"
)
# S1 generic
S1_REGEX = re.compile(
    r"^GRD/[0-9]{4}/[0-9]+/[0-9]+/[A-Z0-9]+/[A-Z0-9]+/(?P<title>S1[A-Z0-9_]+)/(?P<file>.+)$"
)
# CBERS4 generic
CBERS4_REGEX = re.compile(
    r"^GRD/[0-9]{4}/[0-9]+/[0-9]+/[A-Z0-9]+/[A-Z0-9]+/(?P<title>S1[A-Z0-9_]+)/(?P<file>.+)$"
)

# S1 image number conf per polarization ---------------------------------------
S1_IMG_NB_PER_POLAR = {
    "SH": {"HH": 1},
    "SV": {"VV": 1},
    "DH": {"HH": 1, "HV": 2},
    "DV": {"VV": 1, "VH": 2},
    "HH": {"HH": 1},
    "HV": {"HV": 1},
    "VV": {"VV": 1},
    "VH": {"VH": 1},
}

AWS_AUTH_ERROR_MESSAGES = [
    "AccessDenied",
    "InvalidAccessKeyId",
    "SignatureDoesNotMatch",
]


class AwsDownload(Download):
    """Download on AWS using S3 protocol.

    :param provider: provider name
    :type provider: str
    :param config: Download plugin configuration:

        * ``config.base_uri`` (str) - s3 endpoint url
        * ``config.requester_pays`` (bool) - (optional) whether download is done from a
          requester-pays bucket or not
        * ``config.flatten_top_dirs`` (bool) - (optional) flatten directory structure
        * ``config.products`` (dict) - (optional) product_type specific configuration
        * ``config.ignore_assets`` (bool) - (optional) ignore assets and download using downloadLink

    :type config: :class:`~eodag.config.PluginConfig`
    """

    def __init__(self, provider: str, config: PluginConfig) -> None:
        super(AwsDownload, self).__init__(provider, config)
        self.requester_pays = getattr(self.config, "requester_pays", False)
        self.s3_session = None

    def download(
        self,
        product: EOProduct,
        auth: Optional[PluginConfig] = None,
        progress_callback: Optional[ProgressCallback] = None,
        wait: int = DEFAULT_DOWNLOAD_WAIT,
        timeout: int = DEFAULT_DOWNLOAD_TIMEOUT,
        **kwargs: Union[str, bool, Dict[str, Any]],
    ) -> str:
        """Download method for AWS S3 API.

        The product can be downloaded as it is, or as SAFE-formatted product.
        SAFE-build is configured for a given provider and product type.
        If the product title is configured to be updated during download and
        SAFE-formatted, its destination path will be:
        `{outputs_prefix}/{title}`

        :param product: The EO product to download
        :type product: :class:`~eodag.api.product._product.EOProduct`
        :param auth: (optional) The configuration of a plugin of type Authentication
        :type auth: Union[AuthBase, Dict[str, str]]
        :param progress_callback: (optional) A method or a callable object
                                  which takes a current size and a maximum
                                  size as inputs and handle progress bar
                                  creation and update to give the user a
                                  feedback on the download progress
        :type progress_callback: :class:`~eodag.utils.ProgressCallback` or None
        :param kwargs: `outputs_prefix` (str), `extract` (bool), `delete_archive` (bool)
                        and `dl_url_params` (dict) can be provided as additional kwargs
                        and will override any other values defined in a configuration
                        file or with environment variables.
        :type kwargs: Union[str, bool, dict]
        :returns: The absolute path to the downloaded product in the local filesystem
        :rtype: str
        """
        if progress_callback is None:
            logger.info(
                "Progress bar unavailable, please call product.download() instead of plugin.download()"
            )
            progress_callback = ProgressCallback(disable=True)

        # prepare download & create dirs (before updating metadata)
        product_local_path, record_filename = self._download_preparation(
            product, progress_callback=progress_callback, **kwargs
        )
        if not record_filename:
            return product_local_path

        product_conf = getattr(self.config, "products", {}).get(
            product.product_type, {}
        )

        # do not try to build SAFE if asset filter is used
        asset_filter = kwargs.get("asset", None)
        if asset_filter:
            build_safe = False
        else:
            build_safe = product_conf.get("build_safe", False)

        ignore_assets = getattr(self.config, "ignore_assets", False)

        # product conf overrides provider conf for "flatten_top_dirs"
        flatten_top_dirs = product_conf.get(
            "flatten_top_dirs", getattr(self.config, "flatten_top_dirs", False)
        )

        # xtra metadata needed for SAFE product
        self._configure_safe_build(build_safe, product)
        # bucket names and prefixes
        bucket_names_and_prefixes = self._get_bucket_names_and_prefixes(
            product, asset_filter, ignore_assets
        )

        # add complementary urls
        try:
            for complementary_url_key in product_conf.get("complementary_url_key", []):
                bucket_names_and_prefixes.append(
                    self.get_product_bucket_name_and_prefix(
                        product, product.properties[complementary_url_key]
                    )
                )
        except KeyError:
            logger.warning(
                "complementary_url_key %s is missing in %s properties"
                % (complementary_url_key, product.properties["id"])
            )

        # authenticate
        authenticated_objects, s3_objects = self._do_authentication(
            bucket_names_and_prefixes, auth
        )

        # downloadable files
        unique_product_chunks = self._get_unique_products(
            bucket_names_and_prefixes,
            authenticated_objects,
            asset_filter,
            ignore_assets,
            product,
        )

        total_size = sum([p.size for p in unique_product_chunks])

        # download
        progress_callback.reset(total=total_size)
        try:
            for product_chunk in unique_product_chunks:
                try:
                    chunk_rel_path = self.get_chunk_dest_path(
                        product,
                        product_chunk,
                        build_safe=build_safe,
                    )
                except NotAvailableError as e:
                    # out of SAFE format chunk
                    logger.warning(e)
                    continue
                chunk_abs_path = os.path.join(product_local_path, chunk_rel_path)
                chunk_abs_path_dir = os.path.dirname(chunk_abs_path)
                if not os.path.isdir(chunk_abs_path_dir):
                    os.makedirs(chunk_abs_path_dir)

                if not os.path.isfile(chunk_abs_path):
                    product_chunk.Bucket().download_file(
                        product_chunk.key,
                        chunk_abs_path,
                        ExtraArgs=getattr(s3_objects, "_params", {}),
                        Callback=progress_callback,
                    )

        except AuthenticationError as e:
            logger.warning("Unexpected error: %s" % e)
        except ClientError as e:
            self._raise_if_auth_error(e)
            logger.warning("Unexpected error: %s" % e)

        # finalize safe product
        if build_safe and "S2_MSI" in product.product_type:
            self.finalize_s2_safe_product(product_local_path)
        # flatten directory structure
        elif flatten_top_dirs:
            flatten_top_directories(product_local_path)

        if build_safe:
            self.check_manifest_file_list(product_local_path)

        if asset_filter is None:
            # save hash/record file
            with open(record_filename, "w") as fh:
                fh.write(product.remote_location)
            logger.debug("Download recorded in %s", record_filename)

            product.location = path_to_uri(product_local_path)

        return product_local_path

    def _download_preparation(
        self, product: EOProduct, progress_callback: ProgressCallback, **kwargs: Any
    ) -> Tuple[str, Optional[str]]:
        """
        preparation for the download:
        - check if file was already downloaded
        - get file path
        - create directories
        :param product: product to be downloaded
        :type product: EOProduct
        :param progress_callback: progress callback to be used
        :type progress_callback: ProgressCallback
        :param kwargs: additional arguments
        :type kwargs: Any
        :return: local path and file name
        :rtype: Tuple[str, Optional[str]]
        """
        product_local_path, record_filename = self._prepare_download(
            product, progress_callback=progress_callback, **kwargs
        )
        if not product_local_path or not record_filename:
            if product_local_path:
                product.location = path_to_uri(product_local_path)
            return product_local_path, None
        product_local_path = product_local_path.replace(".zip", "")
        # remove existing incomplete file
        if os.path.isfile(product_local_path):
            os.remove(product_local_path)
        # create product dest dir
        if not os.path.isdir(product_local_path):
            os.makedirs(product_local_path)
        return product_local_path, record_filename

    def _configure_safe_build(self, build_safe: bool, product: EOProduct):
        """
        updates the product properties with fetch metadata if safe build is enabled
        :param build_safe: if safe build is enabled
        :type build_safe: bool
        :param product: product to be updated
        :type product: EOProduct
        """
        product_conf = getattr(self.config, "products", {}).get(
            product.product_type, {}
        )
        ssl_verify = getattr(self.config, "ssl_verify", True)
        
        if build_safe and "fetch_metadata" in product_conf.keys():
            fetch_format = product_conf["fetch_metadata"]["fetch_format"]
            update_metadata = product_conf["fetch_metadata"]["update_metadata"]
            fetch_url = product_conf["fetch_metadata"]["fetch_url"].format(
                **product.properties
            )
            logger.info("Fetching extra metadata from %s" % fetch_url)
            try:
                resp = requests.get(
                    fetch_url, headers=USER_AGENT, timeout=HTTP_REQ_TIMEOUT, verify=ssl_verify
                )
            except requests.exceptions.Timeout as exc:
                raise TimeOutError(exc, timeout=HTTP_REQ_TIMEOUT) from exc
            update_metadata = mtd_cfg_as_conversion_and_querypath(update_metadata)
            if fetch_format == "json":
                json_resp = resp.json()
                update_metadata = properties_from_json(json_resp, update_metadata)
                product.properties.update(update_metadata)
            elif fetch_format == "xml":
                update_metadata = properties_from_xml(resp.content, update_metadata)
                product.properties.update(update_metadata)
            else:
                logger.warning(
                    "SAFE metadata fetch format %s not implemented" % fetch_format
                )

    def _get_bucket_names_and_prefixes(
        self, product: EOProduct, asset_filter: str, ignore_assets: bool
    ) -> List[Tuple[str, Optional[str]]]:
        """
        retrieves the bucket names and path prefixes for the assets
        :param product: product for which the assets shall be downloaded
        :type product: EOProduct
        :param asset_filter: text for which the assets should be filtered
        :type asset_filter: str
        :param ignore_assets: if product instead of individual assets should be used
        :type ignore_assets: bool
        :return: tuples of bucket names and prefixes
        :rtype: List[Tuple[str, Optional[str]]]
        """
        # if assets are defined, use them instead of scanning product.location
        if len(product.assets) > 0 and not ignore_assets:
            if asset_filter:
                filter_regex = re.compile(asset_filter)
                assets_keys = getattr(product, "assets", {}).keys()
                assets_keys = list(filter(filter_regex.fullmatch, assets_keys))
                filtered_assets = {
                    a_key: getattr(product, "assets", {})[a_key]
                    for a_key in assets_keys
                }
                assets_values = [a for a in filtered_assets.values() if "href" in a]
                if not assets_values:
                    raise NotAvailableError(
                        rf"No asset key matching re.fullmatch(r'{asset_filter}') was found in {product}"
                    )
            else:
                assets_values = getattr(product, "assets", {}).values()

            bucket_names_and_prefixes = []
            for complementary_url in assets_values:
                bucket_names_and_prefixes.append(
                    self.get_product_bucket_name_and_prefix(
                        product, complementary_url.get("href", "")
                    )
                )
        else:
            bucket_names_and_prefixes = [
                self.get_product_bucket_name_and_prefix(product)
            ]
        return bucket_names_and_prefixes

    def _do_authentication(
        self,
        bucket_names_and_prefixes: List[Tuple[str, Optional[str]]],
        auth: Dict[str, str],
    ) -> Tuple[Dict[str, Any], ResourceCollection[Any]]:
        """
        authenticates with s3 and retrieves the available objects
        raises an error when authentication is not possible
        :param bucket_names_and_prefixes: list of bucket names and corresponding path prefixes
        :type bucket_names_and_prefixes: List[Tuple[str, Optional[str]]]
        :param auth: authentication information
        :type auth: Dict[str, str]
        :return: authenticated objects per bucket, list of available objects
        :rtype: Tuple[Dict[str, Any], ResourceCollection[Any]]
        """
        authenticated_objects: Dict[str, Any] = {}
        auth_error_messages: Set[str] = set()
        for _, pack in enumerate(bucket_names_and_prefixes):
            try:
                bucket_name, prefix = pack
                if bucket_name not in authenticated_objects:
                    # get Prefixes longest common base path
                    common_prefix = ""
                    prefix_split = prefix.split("/")
                    prefixes_in_bucket = len(
                        [p for b, p in bucket_names_and_prefixes if b == bucket_name]
                    )
                    for i in range(1, len(prefix_split)):
                        common_prefix = "/".join(prefix_split[0:i])
                        if (
                            len(
                                [
                                    p
                                    for b, p in bucket_names_and_prefixes
                                    if b == bucket_name and common_prefix in p
                                ]
                            )
                            < prefixes_in_bucket
                        ):
                            common_prefix = "/".join(prefix_split[0 : i - 1])
                            break
                    # connect to aws s3 and get bucket auhenticated objects
                    s3_objects = self.get_authenticated_objects(
                        bucket_name, common_prefix, auth
                    )
                    authenticated_objects[bucket_name] = s3_objects
                else:
                    s3_objects = authenticated_objects[bucket_name]

            except AuthenticationError as e:
                logger.warning("Unexpected error: %s" % e)
                logger.warning("Skipping %s/%s" % (bucket_name, prefix))
                auth_error_messages.add(str(e))
            except ClientError as e:
                self._raise_if_auth_error(e)
                logger.warning("Unexpected error: %s" % e)
                logger.warning("Skipping %s/%s" % (bucket_name, prefix))
                auth_error_messages.add(str(e))

        # could not auth on any bucket
        if not authenticated_objects:
            raise AuthenticationError(", ".join(auth_error_messages))
        return authenticated_objects, s3_objects

    def _get_unique_products(
        self,
        bucket_names_and_prefixes: List[Tuple[str, Optional[str]]],
        authenticated_objects: Dict[str, Any],
        asset_filter: str,
        ignore_assets: bool,
        product: EOProduct,
    ) -> Set[Any]:
        """
        retrieve unique product chunks based on authenticated objects and asset filters
        :param bucket_names_and_prefixes: list of bucket names and corresponding path prefixes
        :type bucket_names_and_prefixes: List[Tuple[str, Optional[str]]]
        :param authenticated_objects: available objects per bucket
        :type authenticated_objects: Dict[str, Any]
        :param asset_filter: text for which assets should be filtered
        :type asset_filter: str
        :param ignore_assets: if product instead of individual assets should be used
        :type ignore_assets: bool
        :param product: product that shall be downloaded
        :type product: EOProduct
        :return: set of product chunks that can be downloaded
        :rtype: Set[Any]
        """
        product_chunks: List[Any] = []
        for bucket_name, prefix in bucket_names_and_prefixes:
            # unauthenticated items filtered out
            if bucket_name in authenticated_objects.keys():
                product_chunks.extend(
                    authenticated_objects[bucket_name].filter(Prefix=prefix)
                )

        unique_product_chunks = set(product_chunks)

        # if asset_filter is used with ignore_assets, apply filtering on listed prefixes
        if asset_filter and ignore_assets:
            filter_regex = re.compile(asset_filter)
            unique_product_chunks = set(
                filter(
                    lambda c: filter_regex.search(os.path.basename(c.key)),
                    unique_product_chunks,
                )
            )
            if not unique_product_chunks:
                raise NotAvailableError(
                    rf"No file basename matching re.fullmatch(r'{asset_filter}') was found in {product.remote_location}"
                )
        return unique_product_chunks

    def _raise_if_auth_error(self, exception: ClientError) -> None:
        """Raises an error if given exception is an authentication error"""
        err = exception.response["Error"]
        if err["Code"] in AWS_AUTH_ERROR_MESSAGES and "key" in err["Message"].lower():
            raise AuthenticationError(
                "HTTP error {} returned\n{}: {}\nPlease check your credentials for {}".format(
                    exception.response["ResponseMetadata"]["HTTPStatusCode"],
                    err["Code"],
                    err["Message"],
                    self.provider,
                )
            )

    def _stream_download_dict(
        self,
        product: EOProduct,
        auth: Optional[PluginConfig] = None,
        progress_callback: Optional[ProgressCallback] = None,
        wait: int = DEFAULT_DOWNLOAD_WAIT,
        timeout: int = DEFAULT_DOWNLOAD_TIMEOUT,
        ssl_verify: bool = True,
        **kwargs: Union[str, bool, Dict[str, Any]],
    ) -> Dict[str, Any]:
        r"""
        Returns dictionnary of :class:`~fastapi.responses.StreamingResponse` keyword-arguments.
        It contains a generator to streamed download chunks and the response headers.

        :param product: The EO product to download
        :type product: :class:`~eodag.api.product._product.EOProduct`
        :param auth: (optional) The configuration of a plugin of type Authentication
        :type auth: :class:`~eodag.config.PluginConfig`
        :param progress_callback: (optional) A progress callback
        :type progress_callback: :class:`~eodag.utils.ProgressCallback`
        :param wait: (optional) If download fails, wait time in minutes between two download tries
        :type wait: int
        :param timeout: (optional) If download fails, maximum time in minutes before stop retrying
                        to download
        :type timeout: int
        :param kwargs: `outputs_prefix` (str), `extract` (bool), `delete_archive` (bool)
                        and `dl_url_params` (dict) can be provided as additional kwargs
                        and will override any other values defined in a configuration
                        file or with environment variables.
        :type kwargs: Union[str, bool, dict]
        :returns: Dictionnary of :class:`~fastapi.responses.StreamingResponse` keyword-arguments
        :rtype: dict
        """
        if progress_callback is None:
            logger.info(
                "Progress bar unavailable, please call product.download() instead of plugin.download()"
            )
            progress_callback = ProgressCallback(disable=True)

        product_conf = getattr(self.config, "products", {}).get(
            product.product_type, {}
        )
        # do not try to build SAFE if asset filter is used
        asset_filter = kwargs.get("asset", None)
        if asset_filter:
            build_safe = False
        else:
            build_safe = product_conf.get("build_safe", False)

        ignore_assets = getattr(self.config, "ignore_assets", False)

        # xtra metadata needed for SAFE product
        self._configure_safe_build(build_safe, product)
        # bucket names and prefixes
        bucket_names_and_prefixes = self._get_bucket_names_and_prefixes(
            product, asset_filter, ignore_assets
        )

        # add complementary urls
        try:
            for complementary_url_key in product_conf.get("complementary_url_key", []):
                bucket_names_and_prefixes.append(
                    self.get_product_bucket_name_and_prefix(
                        product, product.properties[complementary_url_key]
                    )
                )
        except KeyError:
            logger.warning(
                "complementary_url_key %s is missing in %s properties"
                % (complementary_url_key, product.properties["id"])
            )

        # authenticate
        authenticated_objects, s3_objects = self._do_authentication(
            bucket_names_and_prefixes, auth
        )

        # downloadable files
        unique_product_chunks = self._get_unique_products(
            bucket_names_and_prefixes,
            authenticated_objects,
            asset_filter,
            ignore_assets,
            product,
        )
        assets_values = product.assets.get_values(asset_filter)
        chunks_tuples = self._stream_download(
            unique_product_chunks, product, build_safe, progress_callback, assets_values,ssl_verify
        )
        outputs_filename = (
            sanitize(product.properties["title"])
            if "title" in product.properties
            else sanitize(product.properties.get("id", "download"))
        )

        if len(assets_values) == 1:
            first_chunks_tuple = next(chunks_tuples)
            # update headers
            filename = os.path.basename(list(unique_product_chunks)[0].key)
            headers = {"content-disposition": f"attachment; filename={filename}"}
            if assets_values[0].get("type", None):
                headers["content-type"] = assets_values[0]["type"]

            return dict(
                content=chain(iter([first_chunks_tuple]), chunks_tuples),
                headers=headers,
            )
        return dict(
            content=stream_zip(chunks_tuples),
            media_type="application/zip",
            headers={
                "content-disposition": f"attachment; filename={outputs_filename}.zip",
            },
        )

    def _stream_download(
        self,
        unique_product_chunks: Set[Any],
        product: EOProduct,
        build_safe: bool,
        progress_callback: ProgressCallback,
        assets_values: List[Dict[str, Any]],
        ssl_verify: bool = True,
    ) -> Iterator[Tuple[str, datetime, int, Any, Iterator[Any]]]:
        """Yield product data chunks"""

        chunk_size = 4096 * 1024
        modified_at = datetime.now()
        perms = 0o600

        def get_chunk_parts(
            product_chunk: Any, progress_callback: ProgressCallback
        ) -> Any:
            try:

                chunk_start = 0
                chunk_end = chunk_start + chunk_size - 1

                while chunk_start <= product_chunk.size:
                    get_kwargs = (
                        dict(RequestPayer="requester") if self.requester_pays else {}
                    )
                    chunk_part = product_chunk.get(
                        Range=f"bytes={chunk_start}-{chunk_end}", **get_kwargs
                    )["Body"].read()
                    progress_callback(len(chunk_part))
                    chunk_start += chunk_size
                    chunk_end += chunk_size
                    yield chunk_part

            except ClientError as e:
                self._raise_if_auth_error(e)
                raise DownloadError("Unexpected error: %s" % e) from e

        product_conf = getattr(self.config, "products", {}).get(
            product.product_type, {}
        )
        flatten_top_dirs = product_conf.get(
            "flatten_top_dirs", getattr(self.config, "flatten_top_dirs", False)
        )
        common_path = ""
        if flatten_top_dirs:
            common_path = self._get_commonpath(
                product, unique_product_chunks, build_safe
            )

        for product_chunk in unique_product_chunks:
            try:
                chunk_rel_path = self.get_chunk_dest_path(
                    product,
                    product_chunk,
                    build_safe=build_safe,
                )
                if flatten_top_dirs:
                    chunk_rel_path = os.path.join(
                        product.properties["title"],
                        re.sub(rf"^{common_path}/?", "", chunk_rel_path),
                    )

            except NotAvailableError as e:
                # out of SAFE format chunk
                logger.warning(e)
                continue

            if len(assets_values) == 1:
                yield from get_chunk_parts(product_chunk, progress_callback)
            else:
                yield (
                    chunk_rel_path,
                    modified_at,
                    perms,
                    ZIP_AUTO(product_chunk.size),
                    get_chunk_parts(product_chunk, progress_callback),
                )

    def _get_commonpath(
        self, product: EOProduct, product_chunks: Set[Any], build_safe: bool
    ) -> str:
        chunk_paths = []
        for product_chunk in product_chunks:
            chunk_paths.append(
                self.get_chunk_dest_path(product, product_chunk, build_safe=build_safe)
            )
        return os.path.commonpath(chunk_paths)

    def get_rio_env(
        self, bucket_name: str, prefix: str, auth_dict: Dict[str, str]
    ) -> Dict[str, Any]:
        """Get rasterio environment variables needed for data access authentication.

        :param bucket_name: Bucket containg objects
        :type bucket_name: str
        :param prefix: Prefix used to try auth
        :type prefix: str
        :param auth_dict: Dictionnary containing authentication keys
        :type auth_dict: dict
        :returns: The rasterio environement variables
        :rtype: dict
        """
        if self.s3_session is not None:
            if self.requester_pays:
                return {"session": self.s3_session, "requester_pays": True}
            else:
                return {"session": self.s3_session}

        _ = self.get_authenticated_objects(bucket_name, prefix, auth_dict)
        if self.s3_session is not None:
            if self.requester_pays:
                return {"session": self.s3_session, "requester_pays": True}
            else:
                return {"session": self.s3_session}
        else:
            return {"aws_unsigned": True}

    def get_authenticated_objects(
        self, bucket_name: str, prefix: str, auth_dict: Dict[str, str]
    ) -> ResourceCollection:
        """Get boto3 authenticated objects for the given bucket using
        the most adapted auth strategy.
        Also expose ``s3_session`` as class variable if available.

        :param bucket_name: Bucket containg objects
        :type bucket_name: str
        :param prefix: Prefix used to filter objects on auth try
                       (not used to filter returned objects)
        :type prefix: str
        :param auth_dict: Dictionnary containing authentication keys
        :type auth_dict: dict
        :returns: The boto3 authenticated objects
        :rtype: :class:`~boto3.resources.collection.s3.Bucket.objectsCollection`
        """
        auth_methods: List[
            Callable[[str, str, Dict[str, str]], Optional[ResourceCollection]]
        ] = [
            self._get_authenticated_objects_unsigned,
            self._get_authenticated_objects_from_auth_profile,
            self._get_authenticated_objects_from_auth_keys,
            self._get_authenticated_objects_from_env,
        ]
        # skip _get_authenticated_objects_from_env if credentials were filled in eodag conf
        if auth_dict:
            del auth_methods[-1]

        for try_auth_method in auth_methods:
            try:
                s3_objects = try_auth_method(bucket_name, prefix, auth_dict)
                if s3_objects:
                    logger.debug("Auth using %s succeeded", try_auth_method.__name__)
                    return s3_objects
            except ClientError as e:
                if (
                    e.response.get("Error", {}).get("Code", {})
                    in AWS_AUTH_ERROR_MESSAGES
                ):
                    pass
                else:
                    raise e
            except ProfileNotFound:
                pass
            logger.debug("Auth using %s failed", try_auth_method.__name__)

        raise AuthenticationError(
            "Unable do authenticate on s3://%s using any available credendials configuration"
            % bucket_name
        )

    def _get_authenticated_objects_unsigned(
        self, bucket_name: str, prefix: str, auth_dict: Dict[str, str]
    ) -> Optional[ResourceCollection]:
        """Auth strategy using no-sign-request"""

        s3_resource = boto3.resource(  # type: ignore
            service_name="s3", endpoint_url=getattr(self.config, "base_uri", None)
        )
        s3_resource.meta.client.meta.events.register(  # type: ignore
            "choose-signer.s3.*", disable_signing
        )
        objects = s3_resource.Bucket(bucket_name).objects  # type: ignore
        list(objects.filter(Prefix=prefix).limit(1))  # type: ignore
        return objects  # type: ignore

    def _get_authenticated_objects_from_auth_profile(
        self, bucket_name: str, prefix: str, auth_dict: Dict[str, str]
    ) -> Optional[ResourceCollection]:
        """Auth strategy using RequestPayer=requester and ``aws_profile`` from provided credentials"""

        if "profile_name" in auth_dict.keys():
            s3_session = boto3.session.Session(profile_name=auth_dict["profile_name"])  # type: ignore
            s3_resource = s3_session.resource(  # type: ignore
                service_name="s3",
                endpoint_url=getattr(self.config, "base_uri", None),
            )
            if self.requester_pays:
                objects = s3_resource.Bucket(bucket_name).objects.filter(  # type: ignore
                    RequestPayer="requester"
                )
            else:
                objects = s3_resource.Bucket(bucket_name).objects  # type: ignore
            list(objects.filter(Prefix=prefix).limit(1))  # type: ignore
            self.s3_session = s3_session  # type: ignore
            return objects  # type: ignore
        else:
            return None

    def _get_authenticated_objects_from_auth_keys(
        self, bucket_name: str, prefix: str, auth_dict: Dict[str, str]
    ) -> Optional[ResourceCollection]:
        """Auth strategy using RequestPayer=requester and ``aws_access_key_id``/``aws_secret_access_key``
        from provided credentials"""

        if all(k in auth_dict for k in ("aws_access_key_id", "aws_secret_access_key")):
            s3_session = boto3.session.Session(  # type: ignore
                aws_access_key_id=auth_dict["aws_access_key_id"],
                aws_secret_access_key=auth_dict["aws_secret_access_key"],
            )
            s3_resource = s3_session.resource(  # type: ignore
                service_name="s3",
                endpoint_url=getattr(self.config, "base_uri", None),
            )
            if self.requester_pays:
                objects = s3_resource.Bucket(bucket_name).objects.filter(  # type: ignore
                    RequestPayer="requester"
                )
            else:
                objects = s3_resource.Bucket(bucket_name).objects  # type: ignore
            list(objects.filter(Prefix=prefix).limit(1))  # type: ignore
            self.s3_session = s3_session  # type: ignore
            return objects  # type: ignore
        else:
            return None

    def _get_authenticated_objects_from_env(
        self, bucket_name: str, prefix: str, auth_dict: Dict[str, str]
    ) -> Optional[ResourceCollection]:
        """Auth strategy using RequestPayer=requester and current environment"""

        s3_session = boto3.session.Session()  # type: ignore
        s3_resource = s3_session.resource(  # type: ignore
            service_name="s3", endpoint_url=getattr(self.config, "base_uri", None)
        )
        if self.requester_pays:
            objects = s3_resource.Bucket(bucket_name).objects.filter(  # type: ignore
                RequestPayer="requester"
            )
        else:
            objects = s3_resource.Bucket(bucket_name).objects  # type: ignore
        list(objects.filter(Prefix=prefix).limit(1))  # type: ignore
        self.s3_session = s3_session  # type: ignore
        return objects  # type: ignore

    def get_product_bucket_name_and_prefix(
        self, product: EOProduct, url: Optional[str] = None
    ) -> Tuple[str, Optional[str]]:
        """Extract bucket name and prefix from product URL

        :param product: The EO product to download
        :type product: :class:`~eodag.api.product._product.EOProduct`
        :param url: (optional) URL to use as product.location
        :type url: str
        :returns: bucket_name and prefix as str
        :rtype: tuple
        """
        if url is None:
            url = product.location

        bucket_path_level = getattr(self.config, "bucket_path_level", None)

        bucket, prefix = get_bucket_name_and_prefix(
            url=url, bucket_path_level=bucket_path_level
        )

        if bucket is None:
            bucket = (
                getattr(self.config, "products", {})
                .get(product.product_type, {})
                .get("default_bucket", "")
            )

        return bucket, prefix

    def check_manifest_file_list(self, product_path: str) -> None:
        """Checks if products listed in manifest.safe exist"""
        manifest_path_list = [
            os.path.join(d, x)
            for d, _, f in os.walk(product_path)
            for x in f
            if x == "manifest.safe"
        ]
        if len(manifest_path_list) == 0:
            raise FileNotFoundError(
                f"No manifest.safe could be found in {product_path}"
            )
        else:
            safe_path = os.path.dirname(manifest_path_list[0])

        root = etree.parse(os.path.join(safe_path, "manifest.safe")).getroot()
        for safe_file in root.xpath("//fileLocation"):
            safe_file_path = os.path.join(safe_path, safe_file.get("href"))
            if not os.path.isfile(safe_file_path) and "HTML" in safe_file.get("href"):
                # add empty files for missing HTML/*
                Path(safe_file_path).touch()
            elif not os.path.isfile(safe_file_path):
                logger.warning("SAFE build: %s is missing" % safe_file.get("href"))

    def finalize_s2_safe_product(self, product_path: str) -> None:
        """Add missing dirs to downloaded product"""
        try:
            logger.debug("Finalize SAFE product")
            manifest_path_list = [
                os.path.join(d, x)
                for d, _, f in os.walk(product_path)
                for x in f
                if x == "manifest.safe"
            ]
            if len(manifest_path_list) == 0:
                raise FileNotFoundError(
                    f"No manifest.safe could be found in {product_path}"
                )
            else:
                safe_path = os.path.dirname(manifest_path_list[0])

            # create empty missing dirs
            auxdata_path = os.path.join(safe_path, "AUX_DATA")
            if not os.path.isdir(auxdata_path):
                os.makedirs(auxdata_path)
            html_path = os.path.join(safe_path, "HTML")
            if not os.path.isdir(html_path):
                os.makedirs(html_path)
            repinfo_path = os.path.join(safe_path, "rep_info")
            if not os.path.isdir(repinfo_path):
                os.makedirs(repinfo_path)

            # granule tile dirname
            root = etree.parse(os.path.join(safe_path, "manifest.safe")).getroot()
            tile_id = cast(
                str,
                os.path.basename(
                    os.path.dirname(
                        root.xpath("//fileLocation[contains(@href,'MTD_TL.xml')]")[
                            0
                        ].get("href")
                    )
                ),
            )
            granule_folder = os.path.join(safe_path, "GRANULE")
            rename_subfolder(granule_folder, tile_id)

            # datastrip scene dirname
            scene_id = cast(
                str,
                os.path.basename(
                    os.path.dirname(
                        root.xpath("//fileLocation[contains(@href,'MTD_DS.xml')]")[
                            0
                        ].get("href")
                    )
                ),
            )
            datastrip_folder = os.path.join(safe_path, "DATASTRIP")
            rename_subfolder(datastrip_folder, scene_id)
        except Exception as e:
            logger.exception("Could not finalize SAFE product from downloaded data")
            raise DownloadError(e)

    def get_chunk_dest_path(
        self,
        product: EOProduct,
        chunk: Any,
        dir_prefix: Optional[str] = None,
        build_safe: bool = False,
    ) -> str:
        """Get chunk SAFE destination path"""
        if not build_safe:
            if dir_prefix is None:
                dir_prefix = chunk.key
            product_path: str = chunk.key.split(dir_prefix.strip("/") + "/")[-1]
            logger.debug(f"Downloading {chunk.key} to {product_path}")
            return product_path

        title_date1: Optional[str] = None
        title_part3: Optional[str] = None
        ds_dir: Any = 0
        s2_processing_level: str = ""
        s1_title_suffix: Optional[str] = None
        # S2 common
        if product.product_type and "S2_MSI" in product.product_type:
            title_search: Optional[Match[str]] = re.search(
                r"^\w+_\w+_(\w+)_(\w+)_(\w+)_(\w+)_(\w+)$",
                product.properties["title"],
            )
            title_date1 = title_search.group(1) if title_search else None
            title_part3 = title_search.group(4) if title_search else None
            ds_dir_search = re.search(
                r"^.+_(DS_\w+_+\w+_\w+)_\w+.\w+$",
                product.properties.get("originalSceneID", ""),
            )
            ds_dir = ds_dir_search.group(1) if ds_dir_search else 0
            s2_processing_level = product.product_type.split("_")[-1]
        # S1 common
        elif product.product_type == "S1_SAR_GRD":
            s1_title_suffix_search = re.search(
                r"^.+_([A-Z0-9_]+_[A-Z0-9_]+_[A-Z0-9_]+_[A-Z0-9_]+)_\w+$",
                product.properties["title"],
            )
            s1_title_suffix = (
                s1_title_suffix_search.group(1).lower().replace("_", "-")
                if s1_title_suffix_search
                else None
            )

        # S2 L2A Tile files -----------------------------------------------
        if matched := S2L2A_TILE_IMG_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "GRANULE/%s/IMG_DATA/R%s/T%s%s%s_%s_%s_%s.jp2" % (
                found_dict["num"],
                found_dict["res"],
                found_dict["tile1"],
                found_dict["tile2"],
                found_dict["tile3"],
                title_date1,
                found_dict["file"],
                found_dict["res"],
            )
        elif matched := S2L2A_TILE_AUX_DIR_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "GRANULE/%s/AUX_DATA/%s" % (
                found_dict["num"],
                found_dict["file"],
            )
        # S2 L2A QI Masks
        elif matched := S2_TILE_QI_MSK_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "GRANULE/%s/QI_DATA/MSK_%sPRB_%s" % (
                found_dict["num"],
                found_dict["file_base"],
                found_dict["file_suffix"],
            )
        # S2 L2A QI PVI
        elif matched := S2_TILE_QI_PVI_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "GRANULE/%s/QI_DATA/%s_%s_PVI.jp2" % (
                found_dict["num"],
                title_part3,
                title_date1,
            )
        # S2 Tile files ---------------------------------------------------
        elif matched := S2_TILE_PREVIEW_DIR_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "GRANULE/%s/preview/%s" % (
                found_dict["num"],
                found_dict["file"],
            )
        elif matched := S2_TILE_IMG_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "GRANULE/%s/IMG_DATA/T%s%s%s_%s_%s" % (
                found_dict["num"],
                found_dict["tile1"],
                found_dict["tile2"],
                found_dict["tile3"],
                title_date1,
                found_dict["file"],
            )
        elif matched := S2_TILE_THUMBNAIL_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "GRANULE/%s/%s" % (
                found_dict["num"],
                found_dict["file"],
            )
        elif matched := S2_TILE_MTD_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "GRANULE/%s/MTD_TL.xml" % found_dict["num"]
        elif matched := S2_TILE_AUX_DIR_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "GRANULE/%s/AUX_DATA/AUX_%s" % (
                found_dict["num"],
                found_dict["file"],
            )
        elif matched := S2_TILE_QI_DIR_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "GRANULE/%s/QI_DATA/%s" % (
                found_dict["num"],
                found_dict["file"],
            )
        # S2 Tiles generic
        elif matched := S2_TILE_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "GRANULE/%s/%s" % (
                found_dict["num"],
                found_dict["file"],
            )
        # S2 Product files
        elif matched := S2_PROD_DS_MTD_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "DATASTRIP/%s/MTD_DS.xml" % ds_dir
        elif matched := S2_PROD_DS_QI_REPORT_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "DATASTRIP/%s/QI_DATA/%s.xml" % (
                ds_dir,
                found_dict["filename"],
            )
        elif matched := S2_PROD_DS_QI_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "DATASTRIP/%s/QI_DATA/%s" % (
                ds_dir,
                found_dict["file"],
            )
        elif matched := S2_PROD_INSPIRE_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "INSPIRE.xml"
        elif matched := S2_PROD_MTD_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "MTD_MSI%s.xml" % s2_processing_level
        # S2 Product generic
        elif matched := S2_PROD_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "%s" % found_dict["file"]
        # S1 --------------------------------------------------------------
        elif matched := S1_CALIB_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "annotation/calibration/%s-%s-%s-grd-%s-%s-%03d.xml" % (
                found_dict["file_prefix"],
                product.properties["platformSerialIdentifier"].lower(),
                found_dict["file_beam"],
                found_dict["file_pol"],
                s1_title_suffix,
                S1_IMG_NB_PER_POLAR.get(product.properties["polarizationMode"], {}).get(
                    found_dict["file_pol"].upper(), 1
                ),
            )
        elif matched := S1_ANNOT_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "annotation/%s-%s-grd-%s-%s-%03d.xml" % (
                product.properties["platformSerialIdentifier"].lower(),
                found_dict["file_beam"],
                found_dict["file_pol"],
                s1_title_suffix,
                S1_IMG_NB_PER_POLAR.get(product.properties["polarizationMode"], {}).get(
                    found_dict["file_pol"].upper(), 1
                ),
            )
        elif matched := S1_MEAS_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "measurement/%s-%s-grd-%s-%s-%03d.%s" % (
                product.properties["platformSerialIdentifier"].lower(),
                found_dict["file_beam"],
                found_dict["file_pol"],
                s1_title_suffix,
                S1_IMG_NB_PER_POLAR.get(product.properties["polarizationMode"], {}).get(
                    found_dict["file_pol"].upper(), 1
                ),
                found_dict["file_ext"],
            )
        elif matched := S1_REPORT_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "%s.SAFE-%s" % (
                product.properties["title"],
                found_dict["file"],
            )
        # S1 generic
        elif matched := S1_REGEX.match(chunk.key):
            found_dict = matched.groupdict()
            product_path = "%s" % found_dict["file"]
        # out of SAFE format
        else:
            raise NotAvailableError(f"Ignored {chunk.key} out of SAFE matching pattern")

        logger.debug(f"Downloading {chunk.key} to {product_path}")
        return product_path

    def download_all(
        self,
        products: SearchResult,
        auth: Optional[PluginConfig] = None,
        downloaded_callback: Optional[DownloadedCallback] = None,
        progress_callback: Optional[ProgressCallback] = None,
        wait: int = DEFAULT_DOWNLOAD_WAIT,
        timeout: int = DEFAULT_DOWNLOAD_TIMEOUT,
        ssl_verify : bool = True,
        **kwargs: Union[str, bool, Dict[str, Any]],
    ) -> List[str]:
        """
        download_all using parent (base plugin) method
        """
        return super(AwsDownload, self).download_all(
            products,
            auth=auth,
            downloaded_callback=downloaded_callback,
            progress_callback=progress_callback,
            wait=wait,
            timeout=timeout,
            ssl_verify=ssl_verify,
            **kwargs,
        )
