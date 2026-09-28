// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.34;

import {AccessControl} from "@openzeppelin/contracts/access/AccessControl.sol";

/**
 * On-chain anchor registry for Digital Product Passports.
 * <p>
 * EVM port of the Chromia `battery` module. The backend stores the actual
 * passport data off-chain in PostgreSQL and commits only the hashes,
 * identifiers, and lifecycle state here.
 * <p>
 * Anchors are written by the `mint*` functions (MINTER_ROLE only) and are
 * immutable — no function in this contract updates or deletes them.
 * <p>
 * Differences from the Rell version:
 * - Hashes are `bytes32` instead of 64-char hex text. Pass them as
 *   `0x` + the same 64 hex chars.
 * - Timestamps are epoch millis in `uint64`, matching Chromia's `timestamp`.
 *   `anchoredAt` is `block.timestamp * 1000`.
 * - Minters are managed with OpenZeppelin AccessControl roles instead of
 *   the fixed `admin_addresses` module argument.
 * - List queries are paginated and ordered newest-anchored first, because an
 *   unbounded list can exceed the RPC node's eth_call gas cap.
 */
contract DppAnchorRegistry is AccessControl {
    /** Role held by the backend anchoring worker(s). */
    bytes32 public constant MINTER_ROLE = keccak256("MINTER_ROLE");

    bytes32 private constant STATUS_ACTIVE = keccak256("active");
    bytes32 private constant STATUS_RECALLED = keccak256("recalled");
    bytes32 private constant STATUS_RECYCLED = keccak256("recycled");

    // ------------------------------------------------------------------
    // Anchors
    // ------------------------------------------------------------------

    /** On-chain anchor for a battery module DPP. Mirrors Rell `dpp_anchor`. */
    struct DppAnchor {
        /** DPP identifier — what the QR code resolves to. */
        string dppId;
        /** Manufacturer / economic operator (UUID as text). */
        string tenantId;
        /** User within the tenant who triggered the mint (UUID as text). */
        string createdByUserId;
        /** SHA-256 of (uuidBytes(moduleId) || longBytes(moduleCreatedAt)). */
        bytes32 moduleRef;
        /** Physical battery serial number, plaintext, globally unique. */
        string serialNumber;
        /** SHA-256 of canonical(moduleData). The integrity anchor. */
        bytes32 dataRootHash;
        /** DPP schema version under which this DPP was minted. */
        string schemaVersion;
        /** Backend's mint timestamp (epoch millis). */
        uint64 mintedAt;
        /** Chain's view of when the anchor was committed (epoch millis). */
        uint64 anchoredAt;
        /** DPP lifecycle state: "active", "recalled", "recycled". */
        string status;
    }

    /** On-chain anchor for a garment DPP. Mirrors Rell `garment_dpp_anchor`. */
    struct GarmentDppAnchor {
        string dppId;
        string tenantId;
        string createdByUserId;
        /** SHA-256 of (uuidBytes(productId) || longBytes(productCreatedAt)). */
        bytes32 productRef;
        /** Physical garment serial number, plaintext, globally unique. */
        string serialNumber;
        /** SHA-256 of canonical(productData). The integrity anchor. */
        bytes32 dataRootHash;
        string schemaVersion;
        uint64 mintedAt;
        uint64 anchoredAt;
        string status;
    }

    /**
     * On-chain anchor for a supplier cell DPP. Mirrors Rell `cell_dpp_anchor`:
     * the serial is unique per tenant, and `dppHash` covers
     * {templateRef, serialNumber, manufacturingDate}.
     */
    struct CellDppAnchor {
        string dppId;
        /** Supplier tenant that minted this passport (UUID as text). */
        string tenantId;
        string createdByUserId;
        /** SHA-256 of canonical(templateData). Pins the exact design version. */
        bytes32 templateRef;
        /** Stable design identity across template versions (UUID as text). */
        string templateKey;
        /** Supplier-assigned serial. Unique per tenant. */
        string serialNumber;
        /** ISO-8601 (yyyy-MM-dd), byte-identical to the string hashed into dppHash. */
        string manufacturingDate;
        /** SHA-256 of canonical({templateRef, serialNumber, manufacturingDate}). */
        bytes32 dppHash;
        string schemaVersion;
        uint64 mintedAt;
        uint64 anchoredAt;
        string status;
    }

    // ------------------------------------------------------------------
    // Mint inputs (the anchor fields minus anchoredAt, which the chain sets)
    // ------------------------------------------------------------------

    struct DppInput {
        string dppId;
        string tenantId;
        string createdByUserId;
        bytes32 moduleRef;
        string serialNumber;
        bytes32 dataRootHash;
        string schemaVersion;
        uint64 mintedAt;
        string status;
    }

    struct GarmentDppInput {
        string dppId;
        string tenantId;
        string createdByUserId;
        bytes32 productRef;
        string serialNumber;
        bytes32 dataRootHash;
        string schemaVersion;
        uint64 mintedAt;
        string status;
    }

    struct CellDppInput {
        string dppId;
        string tenantId;
        string createdByUserId;
        bytes32 templateRef;
        string templateKey;
        string serialNumber;
        string manufacturingDate;
        bytes32 dppHash;
        string schemaVersion;
        uint64 mintedAt;
        string status;
    }

    // ------------------------------------------------------------------
    // Storage
    // ------------------------------------------------------------------

    mapping(string dppId => DppAnchor) private _dpps;
    mapping(string serialNumber => bool) private _dppSerialTaken;
    mapping(string tenantId => string[]) private _dppIdsByTenant;

    mapping(string dppId => GarmentDppAnchor) private _garmentDpps;
    mapping(string serialNumber => bool) private _garmentSerialTaken;
    mapping(string tenantId => string[]) private _garmentDppIdsByTenant;

    mapping(string dppId => CellDppAnchor) private _cellDpps;
    mapping(bytes32 tenantSerialKey => string) private _cellDppIdByTenantSerial;
    mapping(string tenantId => string[]) private _cellDppIdsByTenant;
    mapping(string templateKey => string[]) private _cellDppIdsByTemplateKey;

    // ------------------------------------------------------------------
    // Events and errors
    // ------------------------------------------------------------------

    event DppAnchored(string indexed tenantId, string dppId, bytes32 dataRootHash, uint64 anchoredAt);
    event GarmentDppAnchored(string indexed tenantId, string dppId, bytes32 dataRootHash, uint64 anchoredAt);
    event CellDppAnchored(
        string indexed tenantId,
        string indexed templateKey,
        string dppId,
        bytes32 dppHash,
        uint64 anchoredAt
    );

    error AlreadyAnchored(string dppId);
    error SerialAlreadyAnchored(string serialNumber);
    error InvalidInput(string reason);

    /**
     * @param admin   Can grant and revoke MINTER_ROLE.
     * @param minters Initial backend worker addresses (the Rell `admin_addresses`).
     */
    constructor(address admin, address[] memory minters) {
        _grantRole(DEFAULT_ADMIN_ROLE, admin);
        for (uint256 i = 0; i < minters.length; i++) {
            _grantRole(MINTER_ROLE, minters[i]);
        }
    }

    // ------------------------------------------------------------------
    // Mint operations
    // ------------------------------------------------------------------

    /**
     * Mint a DPP anchor on-chain. MINTER_ROLE only.
     * <p>
     * Called by the backend's anchoring worker for each pending DPP. Reverts
     * on a duplicate `dppId`; the worker checks `dppExists` before retrying.
     */
    function mintDpp(DppInput calldata input) external onlyRole(MINTER_ROLE) {
        if (_exists(_dpps[input.dppId].dppId)) revert AlreadyAnchored(input.dppId);
        if (_dppSerialTaken[input.serialNumber]) revert SerialAlreadyAnchored(input.serialNumber);

        // Defensive validation. Prevents garbage data from being permanently
        // committed if the backend is ever wrong about the format.
        _requireNonEmpty(input.dppId, "dppId must not be empty");
        _requireHash(input.dataRootHash, "dataRootHash must not be zero");
        _requireHash(input.moduleRef, "moduleRef must not be zero");
        _requireNonEmpty(input.serialNumber, "serialNumber must not be empty");
        _requireStatus(input.status);

        uint64 anchoredAt = _now();
        _dpps[input.dppId] = DppAnchor({
            dppId: input.dppId,
            tenantId: input.tenantId,
            createdByUserId: input.createdByUserId,
            moduleRef: input.moduleRef,
            serialNumber: input.serialNumber,
            dataRootHash: input.dataRootHash,
            schemaVersion: input.schemaVersion,
            mintedAt: input.mintedAt,
            anchoredAt: anchoredAt,
            status: input.status
        });
        _dppSerialTaken[input.serialNumber] = true;
        _dppIdsByTenant[input.tenantId].push(input.dppId);

        emit DppAnchored(input.tenantId, input.dppId, input.dataRootHash, anchoredAt);
    }

    /**
     * Mint a garment DPP anchor on-chain. MINTER_ROLE only.
     * <p>
     * Mirrors `mintDpp` for the textile vertical.
     */
    function mintGarmentDpp(GarmentDppInput calldata input) external onlyRole(MINTER_ROLE) {
        if (_exists(_garmentDpps[input.dppId].dppId)) revert AlreadyAnchored(input.dppId);
        if (_garmentSerialTaken[input.serialNumber]) revert SerialAlreadyAnchored(input.serialNumber);

        _requireNonEmpty(input.dppId, "dppId must not be empty");
        _requireHash(input.dataRootHash, "dataRootHash must not be zero");
        _requireHash(input.productRef, "productRef must not be zero");
        _requireNonEmpty(input.serialNumber, "serialNumber must not be empty");
        _requireStatus(input.status);

        uint64 anchoredAt = _now();
        _garmentDpps[input.dppId] = GarmentDppAnchor({
            dppId: input.dppId,
            tenantId: input.tenantId,
            createdByUserId: input.createdByUserId,
            productRef: input.productRef,
            serialNumber: input.serialNumber,
            dataRootHash: input.dataRootHash,
            schemaVersion: input.schemaVersion,
            mintedAt: input.mintedAt,
            anchoredAt: anchoredAt,
            status: input.status
        });
        _garmentSerialTaken[input.serialNumber] = true;
        _garmentDppIdsByTenant[input.tenantId].push(input.dppId);

        emit GarmentDppAnchored(input.tenantId, input.dppId, input.dataRootHash, anchoredAt);
    }

    /**
     * Mint a supplier cell DPP anchor on-chain. MINTER_ROLE only.
     * <p>
     * Serial collisions are scoped to the supplier (tenant), not the chain.
     */
    function mintCellDpp(CellDppInput calldata input) external onlyRole(MINTER_ROLE) {
        if (_exists(_cellDpps[input.dppId].dppId)) revert AlreadyAnchored(input.dppId);
        bytes32 tenantSerialKey = _tenantSerialKey(input.tenantId, input.serialNumber);
        if (_exists(_cellDppIdByTenantSerial[tenantSerialKey])) {
            revert SerialAlreadyAnchored(input.serialNumber);
        }

        _requireNonEmpty(input.dppId, "dppId must not be empty");
        _requireHash(input.templateRef, "templateRef must not be zero");
        _requireHash(input.dppHash, "dppHash must not be zero");
        _requireNonEmpty(input.serialNumber, "serialNumber must not be empty");
        if (bytes(input.manufacturingDate).length != 10) {
            revert InvalidInput("manufacturingDate must be ISO-8601 yyyy-MM-dd");
        }
        _requireStatus(input.status);

        uint64 anchoredAt = _now();
        _cellDpps[input.dppId] = CellDppAnchor({
            dppId: input.dppId,
            tenantId: input.tenantId,
            createdByUserId: input.createdByUserId,
            templateRef: input.templateRef,
            templateKey: input.templateKey,
            serialNumber: input.serialNumber,
            manufacturingDate: input.manufacturingDate,
            dppHash: input.dppHash,
            schemaVersion: input.schemaVersion,
            mintedAt: input.mintedAt,
            anchoredAt: anchoredAt,
            status: input.status
        });
        _cellDppIdByTenantSerial[tenantSerialKey] = input.dppId;
        _cellDppIdsByTenant[input.tenantId].push(input.dppId);
        _cellDppIdsByTemplateKey[input.templateKey].push(input.dppId);

        emit CellDppAnchored(input.tenantId, input.templateKey, input.dppId, input.dppHash, anchoredAt);
    }

    // ------------------------------------------------------------------
    // Battery DPP queries
    // ------------------------------------------------------------------

    /**
     * Look up a DPP anchor by its dppId. Used by public verifiers to compare
     * the on-chain commitment against the off-chain data. Returns an anchor
     * with an empty `dppId` if no such DPP has been anchored.
     */
    function getDppAnchor(string calldata dppId) external view returns (DppAnchor memory) {
        return _dpps[dppId];
    }

    /** Cheap idempotency check used by the backend worker before (re)submitting. */
    function dppExists(string calldata dppId) external view returns (bool) {
        return _exists(_dpps[dppId].dppId);
    }

    function getDppAnchorCountByTenant(string calldata tenantId) external view returns (uint256) {
        return _dppIdsByTenant[tenantId].length;
    }

    /** A page of a tenant's DPP anchors, newest-anchored first. */
    function getDppAnchorsByTenant(string calldata tenantId, uint256 offset, uint256 limit)
        external
        view
        returns (DppAnchor[] memory page)
    {
        string[] storage ids = _dppIdsByTenant[tenantId];
        uint256 n = _pageSize(ids.length, offset, limit);
        page = new DppAnchor[](n);
        for (uint256 i = 0; i < n; i++) {
            page[i] = _dpps[ids[ids.length - 1 - offset - i]];
        }
    }

    // ------------------------------------------------------------------
    // Garment DPP queries
    // ------------------------------------------------------------------

    function getGarmentDppAnchor(string calldata dppId) external view returns (GarmentDppAnchor memory) {
        return _garmentDpps[dppId];
    }

    function garmentDppExists(string calldata dppId) external view returns (bool) {
        return _exists(_garmentDpps[dppId].dppId);
    }

    function getGarmentDppAnchorCountByTenant(string calldata tenantId) external view returns (uint256) {
        return _garmentDppIdsByTenant[tenantId].length;
    }

    function getGarmentDppAnchorsByTenant(string calldata tenantId, uint256 offset, uint256 limit)
        external
        view
        returns (GarmentDppAnchor[] memory page)
    {
        string[] storage ids = _garmentDppIdsByTenant[tenantId];
        uint256 n = _pageSize(ids.length, offset, limit);
        page = new GarmentDppAnchor[](n);
        for (uint256 i = 0; i < n; i++) {
            page[i] = _garmentDpps[ids[ids.length - 1 - offset - i]];
        }
    }

    // ------------------------------------------------------------------
    // Cell DPP queries
    // ------------------------------------------------------------------

    /** The public QR-verification path for cells. */
    function getCellDppAnchor(string calldata dppId) external view returns (CellDppAnchor memory) {
        return _cellDpps[dppId];
    }

    /**
     * The recovery path: find a cell passport by supplier + serial when the QR
     * label is damaged but the serial is still legible.
     */
    function getCellDppAnchorBySerial(string calldata tenantId, string calldata serialNumber)
        external
        view
        returns (CellDppAnchor memory)
    {
        return _cellDpps[_cellDppIdByTenantSerial[_tenantSerialKey(tenantId, serialNumber)]];
    }

    function cellDppExists(string calldata dppId) external view returns (bool) {
        return _exists(_cellDpps[dppId].dppId);
    }

    function getCellDppAnchorCountByTenant(string calldata tenantId) external view returns (uint256) {
        return _cellDppIdsByTenant[tenantId].length;
    }

    function getCellDppAnchorsByTenant(string calldata tenantId, uint256 offset, uint256 limit)
        external
        view
        returns (CellDppAnchor[] memory)
    {
        return _cellPage(_cellDppIdsByTenant[tenantId], offset, limit);
    }

    function getCellDppAnchorCountByTemplateKey(string calldata templateKey) external view returns (uint256) {
        return _cellDppIdsByTemplateKey[templateKey].length;
    }

    /**
     * Every cell anchored to one design, newest-anchored first. Lets an OEM
     * verify that the cells it aggregates into a module share a design.
     */
    function getCellDppAnchorsByTemplateKey(string calldata templateKey, uint256 offset, uint256 limit)
        external
        view
        returns (CellDppAnchor[] memory)
    {
        return _cellPage(_cellDppIdsByTemplateKey[templateKey], offset, limit);
    }

    // ------------------------------------------------------------------
    // Internal helpers
    // ------------------------------------------------------------------

    function _cellPage(string[] storage ids, uint256 offset, uint256 limit)
        private
        view
        returns (CellDppAnchor[] memory page)
    {
        uint256 n = _pageSize(ids.length, offset, limit);
        page = new CellDppAnchor[](n);
        for (uint256 i = 0; i < n; i++) {
            page[i] = _cellDpps[ids[ids.length - 1 - offset - i]];
        }
    }

    function _pageSize(uint256 total, uint256 offset, uint256 limit) private pure returns (uint256) {
        if (offset >= total) return 0;
        uint256 remaining = total - offset;
        return remaining < limit ? remaining : limit;
    }

    function _tenantSerialKey(string calldata tenantId, string calldata serialNumber)
        private
        pure
        returns (bytes32)
    {
        return keccak256(abi.encode(tenantId, serialNumber));
    }

    function _now() private view returns (uint64) {
        return uint64(block.timestamp) * 1000;
    }

    function _exists(string storage dppId) private view returns (bool) {
        return bytes(dppId).length != 0;
    }

    function _requireNonEmpty(string calldata value, string memory reason) private pure {
        if (bytes(value).length == 0) revert InvalidInput(reason);
    }

    function _requireHash(bytes32 value, string memory reason) private pure {
        if (value == bytes32(0)) revert InvalidInput(reason);
    }

    function _requireStatus(string calldata status) private pure {
        bytes32 s = keccak256(bytes(status));
        if (s != STATUS_ACTIVE && s != STATUS_RECALLED && s != STATUS_RECYCLED) {
            revert InvalidInput("status must be one of: active, recalled, recycled");
        }
    }
}
