/* Bounded BP1 byte transposition. No allocation, I/O, Python API or floats. */
#include <stddef.h>
#include <stdint.h>
#ifdef _WIN32
#define CW_EXPORT __declspec(dllexport)
#else
#define CW_EXPORT __attribute__((visibility("default")))
#endif

#define CW_NF_SLOTS 4096u
#define CW_NF_WIDTH 34u
#define CW_NF_BYTES (CW_NF_SLOTS * CW_NF_WIDTH)

CW_EXPORT uint32_t cw_nf_abi(void) { return 2u; }

CW_EXPORT int cw_nf_untranspose(const unsigned char *lengths, size_t count,
                      const unsigned char *planes, size_t plane_size,
                      unsigned char *output, size_t output_size) {
    uint32_t offsets[CW_NF_SLOTS + 1u];
    uint32_t total = 0u;
    unsigned int width = 0u;
    size_t cursor = 0u;
    if (!lengths || !planes || !output || count == 0u ||
        count > CW_NF_SLOTS || plane_size == 0u ||
        plane_size > CW_NF_BYTES || output_size < plane_size) {
        return 0;
    }
    offsets[0] = 0u;
    /* Complete validation before touching the caller's output buffer. */
    for (size_t i = 0u; i < count; ++i) {
        unsigned int size = lengths[i];
        if (size > CW_NF_WIDTH || total > CW_NF_BYTES - size) {
            return 0;
        }
        total += size;
        offsets[i + 1u] = total;
        if (size > width) width = size;
    }
    if ((size_t)total != plane_size) return 0;
    for (unsigned int column = 0u; column < width; ++column) {
        for (size_t slot = 0u; slot < count; ++slot) {
            if (lengths[slot] > column) {
                output[offsets[slot] + column] = planes[cursor++];
            }
        }
    }
    return cursor == plane_size;
}
