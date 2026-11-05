/**
 * The omp extension Ember loads (``-e``) when a run opts into deferred tools.
 *
 * omp's built-in ``read`` and ``write`` are the transport for ``xd://`` devices
 * (``read xd://<tool>`` returns a schema, ``write xd://<tool>`` runs the tool),
 * but on their own they also read and write the filesystem and fetch URLs. This
 * guard blocks every call except the two uses the transport exists for:
 *
 * - ``read`` of ``xd://``, ``xd://<device>`` or ``xd://<device>/<topic>``;
 * - ``write`` to ``xd://<device>``;
 * - the device's own call, which omp raises as a nested ``tool_call`` under the
 *   device's name once the write is dispatched.
 *
 * ``<device>`` must be one of the names in ``ANNEALAGE_XD_DEVICES`` (a JSON
 * array the host sets), so the built-in devices (``xd://resolve``,
 * ``xd://propose``, ``xd://eval`` and the rest) and any tool the host did not
 * register are refused too. Every other tool name is refused.
 *
 * The check is a whole-string match on the lower-case ``xd://`` spelling only.
 * omp trims and case-folds the scheme before it routes, but anything this
 * pattern does not recognise is refused rather than interpreted, so a spelling
 * omp would accept and this guard would not (leading space, ``XD://``, a query,
 * a fragment, a second slash) is denied.
 *
 * It fails closed. A missing or malformed device list leaves an empty set that
 * refuses everything, and a handler that throws or times out is turned into a
 * block by omp. It does NOT fail closed on a load failure: omp skips an ``-e``
 * extension that is missing, does not parse or throws while loading, and starts
 * unguarded without a word. The last thing the factory does is register the
 * ``annealage-xd-guard`` command, so the host asks ``get_available_commands``
 * after start and refuses to run a prompt unless ``GUARD_COMMAND`` is listed with
 * ``source: "extension"`` (a ``.omp/commands/<name>.md`` file in the working directory
 * is listed under the same name, as ``file``).
 */

const GUARD_COMMAND = "annealage-xd-guard";

const SEGMENT = "[A-Za-z0-9_.-]+";
const READ_PATH = new RegExp(`^xd://(?:(${SEGMENT})(?:/(${SEGMENT}))?)?$`);
const WRITE_PATH = new RegExp(`^xd://(${SEGMENT})$`);

/** The device names the host set, or null when they are missing or malformed. */
function loadDevices() {
	try {
		const names = JSON.parse(process.env.ANNEALAGE_XD_DEVICES ?? "null");
		if (Array.isArray(names) && names.every((name) => typeof name === "string" && name.length > 0)) {
			return new Set(names);
		}
	} catch {
		// Falls through to null: no device list means no device.
	}
	return null;
}

function block(reason) {
	return { block: true, reason: `Blocked: ${reason}` };
}

export default function (pi) {
	const devices = loadDevices();
	pi.on("tool_call", async (event) => {
		if (devices === null) return block("the device list for this session is missing or malformed");
		const input = event.input;
		const path = input !== null && typeof input === "object" ? input.path : undefined;
		if (event.toolName === "read") {
			const keys = Object.keys(input ?? {});
			if (typeof path !== "string" || keys.length !== 1) {
				return block("read takes one xd:// path and nothing else");
			}
			const match = READ_PATH.exec(path);
			if (match === null) return block("read is limited to xd:// device discovery");
			if (match[1] !== undefined && !devices.has(match[1])) {
				return block(`xd://${match[1]} is not a device in this session`);
			}
			return undefined;
		}
		if (event.toolName === "write") {
			const keys = Object.keys(input ?? {}).sort();
			if (typeof path !== "string" || keys.join() !== "content,path" || typeof input.content !== "string") {
				return block("write takes an xd:// path and its content, nothing else");
			}
			const match = WRITE_PATH.exec(path);
			if (match === null) return block("write is limited to xd:// device calls");
			if (!devices.has(match[1])) return block(`xd://${match[1]} is not a device in this session`);
			return undefined;
		}
		if (devices.has(event.toolName)) return undefined;
		return block(`${event.toolName} is not available in this session`);
	});
	pi.registerCommand(GUARD_COMMAND, {
		description: "Present when the xd:// guard is loaded",
		handler: async () => {},
	});
}
