// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/**
 * On-chain registry of Digital Product Passports, for any kind of product.
 * <p>
 * The passport itself (a JSON document) lives off-chain. This contract anchors
 * its SHA-256 hash together with the identifiers needed to find it and its
 * lifecycle status, so anyone can check that a passport shown to them is the
 * one that was issued, and has not been altered since.
 * <p>
 * A passport is created once and can then be updated (new data, a recall, end
 * of life). Every update appends a version; earlier versions stay readable.
 * <p>
 * Writes are restricted to minters (the issuing backend). An admin manages the
 * minter list and can hand the admin role over in two steps.
 */
contract DppRegistry {
    // ------------------------------------------------------------------
    // Types
    // ------------------------------------------------------------------

    /** One state of a passport. Version 1 is the one it was created with. */
    struct Version {
        /** SHA-256 of the canonical passport document. The integrity anchor. */
        bytes32 dataHash;
        /** Version of the passport schema the document follows. */
        string schemaVersion;
        /** Where the full passport can be read (optional). */
        string uri;
        /** Lifecycle status, e.g. "active", "recalled", "recycled". */
        string status;
        /** When the issuer produced this version (epoch millis). */
        uint64 issuedAt;
        /** When the chain recorded it (epoch millis, from the block time). */
        uint64 anchoredAt;
    }

    /** A passport with its latest version, as returned by the queries. */
    struct Passport {
        /** Passport identifier — what a QR code or data carrier resolves to. */
        string dppId;
        /** The economic operator that issued the passport. */
        string tenantId;
        /** Free-form product category, e.g. "battery", "textile", "electronics". */
        string productType;
        /** Product serial number. Unique per tenant and product type. */
        string serialNumber;
        /** Number of versions; the fields below are those of the latest. */
        uint32 version;
        bytes32 dataHash;
        string schemaVersion;
        string uri;
        string status;
        uint64 issuedAt;
        uint64 anchoredAt;
        /** When version 1 was recorded (epoch millis). */
        uint64 createdAt;
    }

    struct NewPassport {
        string dppId;
        string tenantId;
        string productType;
        string serialNumber;
        bytes32 dataHash;
        string schemaVersion;
        string uri;
        string status;
        uint64 issuedAt;
    }

    struct PassportUpdate {
        string dppId;
        bytes32 dataHash;
        string schemaVersion;
        string uri;
        string status;
        uint64 issuedAt;
    }

    struct Identity {
        string tenantId;
        string productType;
        string serialNumber;
    }

    // ------------------------------------------------------------------
    // Storage
    // ------------------------------------------------------------------

    /** Most passports one createPassports call accepts, keeping a batch well inside block gas limits. */
    uint256 public constant MAX_BATCH = 100;

    address public admin;
    address public pendingAdmin;
    mapping(address account => bool) public isMinter;

    mapping(string dppId => Identity) private _identities;
    mapping(string dppId => Version[]) private _versions;
    mapping(bytes32 serialKey => string dppId) private _dppIdBySerial;
    mapping(string tenantId => string[]) private _dppIdsByTenant;

    // ------------------------------------------------------------------
    // Events and errors
    // ------------------------------------------------------------------

    event PassportCreated(
        string indexed tenantIdIndex,
        string dppId,
        string productType,
        bytes32 dataHash,
        uint64 anchoredAt
    );
    event PassportUpdated(string dppId, uint32 version, bytes32 dataHash, string status, uint64 anchoredAt);
    event MinterSet(address indexed account, bool allowed);
    event AdminTransferStarted(address indexed currentAdmin, address indexed newAdmin);
    event AdminTransferred(address indexed previousAdmin, address indexed newAdmin);

    error NotAdmin(address account);
    error NotMinter(address account);
    error AlreadyExists(string dppId);
    error SerialTaken(string serialNumber);
    error NotFound(string dppId);
    error InvalidInput(string reason);

    modifier onlyAdmin() {
        if (msg.sender != admin) revert NotAdmin(msg.sender);
        _;
    }

    modifier onlyMinter() {
        if (!isMinter[msg.sender]) revert NotMinter(msg.sender);
        _;
    }

    /**
     * @param admin_  Manages minters. Use a wallet that is not on the server.
     * @param minters Initial issuing backend addresses.
     */
    constructor(address admin_, address[] memory minters) {
        if (admin_ == address(0)) revert InvalidInput("admin must not be zero");
        admin = admin_;
        emit AdminTransferred(address(0), admin_);
        for (uint256 i = 0; i < minters.length; i++) {
            isMinter[minters[i]] = true;
            emit MinterSet(minters[i], true);
        }
    }

    // ------------------------------------------------------------------
    // Writes
    // ------------------------------------------------------------------

    /** Creates a passport. Minters only. */
    function createPassport(NewPassport calldata p) external onlyMinter {
        if (_versions[p.dppId].length != 0) revert AlreadyExists(p.dppId);
        _create(p);
    }

    /**
     * Creates several passports in one transaction. Minters only.
     * <p>
     * Idempotent, so an issuer can resend a batch whose first attempt had an
     * unknown outcome (sent, then timed out): a passport that already exists as
     * the same passport — same identity and version-1 hash — is skipped. One
     * that exists as a different passport is a real conflict and reverts the
     * whole batch, as does any invalid entry. Only created passports emit
     * PassportCreated, so the receipt says which ones this call wrote.
     */
    function createPassports(NewPassport[] calldata ps) external onlyMinter {
        if (ps.length == 0 || ps.length > MAX_BATCH) revert InvalidInput("batch size out of range");
        for (uint256 i = 0; i < ps.length; i++) {
            NewPassport calldata p = ps[i];
            Version[] storage existing = _versions[p.dppId];
            if (existing.length == 0) {
                _create(p);
            } else if (existing[0].dataHash != p.dataHash || !_sameIdentity(_identities[p.dppId], p)) {
                revert AlreadyExists(p.dppId);
            }
        }
    }

    /** Appends a new version to an existing passport. Minters only. */
    function updatePassport(PassportUpdate calldata u) external onlyMinter {
        if (_versions[u.dppId].length == 0) revert NotFound(u.dppId);
        uint64 anchoredAt = _appendVersion(u.dppId, u.dataHash, u.schemaVersion, u.uri, u.status, u.issuedAt);
        emit PassportUpdated(u.dppId, uint32(_versions[u.dppId].length), u.dataHash, u.status, anchoredAt);
    }

    function setMinter(address account, bool allowed) external onlyAdmin {
        isMinter[account] = allowed;
        emit MinterSet(account, allowed);
    }

    /** Step 1 of handing over the admin role; `newAdmin` must accept. */
    function transferAdmin(address newAdmin) external onlyAdmin {
        pendingAdmin = newAdmin;
        emit AdminTransferStarted(admin, newAdmin);
    }

    /** Step 2: the pending admin takes over. */
    function acceptAdmin() external {
        if (msg.sender != pendingAdmin || msg.sender == address(0)) revert NotAdmin(msg.sender);
        emit AdminTransferred(admin, msg.sender);
        admin = msg.sender;
        pendingAdmin = address(0);
    }

    // ------------------------------------------------------------------
    // Queries
    // ------------------------------------------------------------------

    /** The passport with its latest version; `dppId` is empty if there is none. */
    function getPassport(string calldata dppId) external view returns (Passport memory) {
        return _passport(dppId);
    }

    function passportExists(string calldata dppId) external view returns (bool) {
        return _versions[dppId].length != 0;
    }

    /** Which of these passports exist, in the order given. One call instead of one per passport. */
    function passportsExist(string[] calldata dppIds) external view returns (bool[] memory exists) {
        exists = new bool[](dppIds.length);
        for (uint256 i = 0; i < dppIds.length; i++) {
            exists[i] = _versions[dppIds[i]].length != 0;
        }
    }

    /** Recovery path: find a passport by its product's serial number. */
    function getPassportBySerial(
        string calldata tenantId,
        string calldata productType,
        string calldata serialNumber
    ) external view returns (Passport memory) {
        return _passport(_dppIdBySerial[_serialKey(tenantId, productType, serialNumber)]);
    }

    /** A page of a passport's versions, oldest first (version 1 at offset 0). */
    function getVersions(string calldata dppId, uint256 offset, uint256 limit)
        external
        view
        returns (Version[] memory page)
    {
        Version[] storage all = _versions[dppId];
        uint256 n = _pageSize(all.length, offset, limit);
        page = new Version[](n);
        for (uint256 i = 0; i < n; i++) {
            page[i] = all[offset + i];
        }
    }

    function getPassportCountByTenant(string calldata tenantId) external view returns (uint256) {
        return _dppIdsByTenant[tenantId].length;
    }

    /** A page of a tenant's passports, newest first. */
    function getPassportsByTenant(string calldata tenantId, uint256 offset, uint256 limit)
        external
        view
        returns (Passport[] memory page)
    {
        string[] storage ids = _dppIdsByTenant[tenantId];
        uint256 n = _pageSize(ids.length, offset, limit);
        page = new Passport[](n);
        for (uint256 i = 0; i < n; i++) {
            page[i] = _passport(ids[ids.length - 1 - offset - i]);
        }
    }

    // ------------------------------------------------------------------
    // Internal
    // ------------------------------------------------------------------

    /** Creates a passport that is known not to exist yet. */
    function _create(NewPassport calldata p) private {
        _requireNonEmpty(p.dppId, "dppId must not be empty");
        _requireNonEmpty(p.tenantId, "tenantId must not be empty");
        _requireNonEmpty(p.productType, "productType must not be empty");
        _requireNonEmpty(p.serialNumber, "serialNumber must not be empty");

        bytes32 serialKey = _serialKey(p.tenantId, p.productType, p.serialNumber);
        if (bytes(_dppIdBySerial[serialKey]).length != 0) revert SerialTaken(p.serialNumber);

        _identities[p.dppId] = Identity(p.tenantId, p.productType, p.serialNumber);
        _dppIdBySerial[serialKey] = p.dppId;
        _dppIdsByTenant[p.tenantId].push(p.dppId);
        uint64 anchoredAt = _appendVersion(p.dppId, p.dataHash, p.schemaVersion, p.uri, p.status, p.issuedAt);

        emit PassportCreated(p.tenantId, p.dppId, p.productType, p.dataHash, anchoredAt);
        emit PassportUpdated(p.dppId, 1, p.dataHash, p.status, anchoredAt);
    }

    function _sameIdentity(Identity storage id, NewPassport calldata p) private view returns (bool) {
        return keccak256(bytes(id.tenantId)) == keccak256(bytes(p.tenantId))
            && keccak256(bytes(id.productType)) == keccak256(bytes(p.productType))
            && keccak256(bytes(id.serialNumber)) == keccak256(bytes(p.serialNumber));
    }

    function _appendVersion(
        string calldata dppId,
        bytes32 dataHash,
        string calldata schemaVersion,
        string calldata uri,
        string calldata status,
        uint64 issuedAt
    ) private returns (uint64 anchoredAt) {
        if (dataHash == bytes32(0)) revert InvalidInput("dataHash must not be zero");
        _requireNonEmpty(status, "status must not be empty");
        anchoredAt = uint64(block.timestamp) * 1000;
        _versions[dppId].push(Version(dataHash, schemaVersion, uri, status, issuedAt, anchoredAt));
    }

    function _passport(string memory dppId) private view returns (Passport memory p) {
        Version[] storage all = _versions[dppId];
        if (all.length == 0) return p;
        Identity storage id = _identities[dppId];
        Version storage latest = all[all.length - 1];
        p = Passport({
            dppId: dppId,
            tenantId: id.tenantId,
            productType: id.productType,
            serialNumber: id.serialNumber,
            version: uint32(all.length),
            dataHash: latest.dataHash,
            schemaVersion: latest.schemaVersion,
            uri: latest.uri,
            status: latest.status,
            issuedAt: latest.issuedAt,
            anchoredAt: latest.anchoredAt,
            createdAt: all[0].anchoredAt
        });
    }

    function _serialKey(string calldata tenantId, string calldata productType, string calldata serialNumber)
        private
        pure
        returns (bytes32)
    {
        return keccak256(abi.encode(tenantId, productType, serialNumber));
    }

    function _pageSize(uint256 total, uint256 offset, uint256 limit) private pure returns (uint256) {
        if (offset >= total) return 0;
        uint256 remaining = total - offset;
        return remaining < limit ? remaining : limit;
    }

    function _requireNonEmpty(string calldata value, string memory reason) private pure {
        if (bytes(value).length == 0) revert InvalidInput(reason);
    }
}
