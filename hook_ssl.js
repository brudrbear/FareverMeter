// hook_ssl.js
// Hooks Farever's ssl.hdll plaintext send/recv wrappers.
//
// HashLink SSL wrapper signature (from libs/ssl/ssl.c):
//   int ssl_send(hl_ssl* ctx, vbyte* buf, int pos, int len)
//   int ssl_recv(hl_ssl* ctx, vbyte* buf, int pos, int len)
// Exports are prefixed by HL_NAME macro -> "ssl_ssl_send" / "ssl_ssl_recv".

const MODULE_NAME = "ssl.hdll";

function hookModule(m) {
    const sendAddr = m.getExportByName("ssl_ssl_send");
    const recvAddr = m.getExportByName("ssl_ssl_recv");

    Interceptor.attach(sendAddr, {
        onEnter(args) {
            this.buf = args[1];
            this.pos = args[2].toInt32();
        },
        onLeave(retval) {
            const written = retval.toInt32();
            if (written > 0) {
                const data = this.buf.add(this.pos).readByteArray(written);
                send({ dir: "tx", len: written, ts: Date.now() }, data);
            }
        }
    });
    console.log("[+] Hooked ssl_ssl_send @ " + sendAddr);

    Interceptor.attach(recvAddr, {
        onEnter(args) {
            this.buf = args[1];
            this.pos = args[2].toInt32();
        },
        onLeave(retval) {
            const read = retval.toInt32();
            if (read > 0) {
                const data = this.buf.add(this.pos).readByteArray(read);
                send({ dir: "rx", len: read, ts: Date.now() }, data);
            }
        }
    });
    console.log("[+] Hooked ssl_ssl_recv @ " + recvAddr);
}

function waitForModule() {
    const m = Process.findModuleByName(MODULE_NAME);
    if (m) {
        console.log("[+] Found " + MODULE_NAME + " base=" + m.base + " size=" + m.size);
        hookModule(m);
    } else {
        console.log("[.] " + MODULE_NAME + " not loaded yet, polling...");
        setTimeout(waitForModule, 250);
    }
}

waitForModule();
