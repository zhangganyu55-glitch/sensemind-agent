package com.sensemind.app.ble

import android.annotation.SuppressLint
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothGatt
import android.bluetooth.BluetoothGattCallback
import android.bluetooth.BluetoothGattCharacteristic
import android.bluetooth.BluetoothGattDescriptor
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.bluetooth.le.ScanCallback
import android.bluetooth.le.ScanFilter
import android.bluetooth.le.ScanResult
import android.bluetooth.le.ScanSettings
import android.content.Context
import android.os.Handler
import android.os.Looper
import android.os.ParcelUuid
import java.util.UUID

data class BleDeviceInfo(val name: String, val address: String, val rssi: Int)

/**
 * Android BLE central for the frozen nRF52840 transport. The firmware source
 * defines the service/characteristics above; physical connection, reconnect and
 * throughput remain unverified until a real board is available.
 *
 * Only one BLE central can hold the peripheral connection. If a Mac gateway is
 * connected, Android connect attempts will fail or the board will not advertise;
 * stop the gateway before scanning here. This client never changes firmware.
 */
class NrfBleClient(private val context: Context) {
    private val main = Handler(Looper.getMainLooper())
    private val serviceUuid = UUID.fromString(BleProtocol.SERVICE_UUID)
    private val dataUuid = UUID.fromString(BleProtocol.DATA_UUID)
    private val ackUuid = UUID.fromString(BleProtocol.ACK_UUID)
    private var gatt: BluetoothGatt? = null
    private var ackCharacteristic: BluetoothGattCharacteristic? = null
    private var scanner: android.bluetooth.le.BluetoothLeScanner? = null
    private var scanning = false
    private val assembler = FragmentAssembler()

    private val scanCallback = object : ScanCallback() {
        override fun onScanResult(callbackType: Int, result: ScanResult) {
            val name = result.scanRecord?.deviceName ?: result.device.name ?: "未命名设备"
            main.post {
                listeners.onDevice?.invoke(
                    BleDeviceInfo(name, result.device.address, result.rssi))
            }
        }

        override fun onScanFailed(errorCode: Int) {
            scanning = false
            main.post { listeners.onError?.invoke("BLE 扫描失败：$errorCode") }
        }
    }

    private var listeners = Callbacks()

    data class Callbacks(
        val onDevice: ((BleDeviceInfo) -> Unit)? = null,
        val onState: ((String) -> Unit)? = null,
        val onPacket: ((Long, String) -> Unit)? = null,
        val onError: ((String) -> Unit)? = null,
    )

    @SuppressLint("MissingPermission")
    fun startScan(
        onDevice: (BleDeviceInfo) -> Unit,
        onError: (String) -> Unit,
    ) {
        val adapter = context.getSystemService(BluetoothManager::class.java)?.adapter
        if (adapter == null || !adapter.isEnabled) {
            onError("蓝牙未开启或被系统禁用")
            return
        }
        if (scanning) return
        listeners = listeners.copy(onDevice = onDevice, onError = onError)
        scanner = adapter.bluetoothLeScanner
        val filter = ScanFilter.Builder()
            .setServiceUuid(ParcelUuid(serviceUuid))
            .build()
        val settings = ScanSettings.Builder()
            .setScanMode(ScanSettings.SCAN_MODE_LOW_LATENCY)
            .build()
        scanning = true
        try {
            scanner?.startScan(listOf(filter), settings, scanCallback)
        } catch (error: SecurityException) {
            scanning = false
            onError("缺少蓝牙扫描权限")
        }
    }

    @SuppressLint("MissingPermission")
    fun stopScan() {
        if (!scanning) return
        scanning = false
        try {
            scanner?.stopScan(scanCallback)
        } catch (_: SecurityException) {
            // Permission may have been revoked; nothing else to clean up.
        }
    }

    @SuppressLint("MissingPermission")
    fun connect(
        address: String,
        onState: (String) -> Unit,
        onPacket: (Long, String) -> Unit,
        onError: (String) -> Unit,
    ) {
        val adapter = context.getSystemService(BluetoothManager::class.java)?.adapter
        val device = adapter?.getRemoteDevice(address)
        if (device == null) {
            onError("找不到 BLE 设备地址")
            return
        }
        listeners = Callbacks(onState = onState, onPacket = onPacket, onError = onError)
        stopScan()
        gatt?.close()
        try {
            gatt = device.connectGatt(context, false, gattCallback, BluetoothDevice.TRANSPORT_LE)
        } catch (error: SecurityException) {
            onError("缺少蓝牙连接权限")
        }
    }

    private val gattCallback = object : BluetoothGattCallback() {
        @SuppressLint("MissingPermission")
        override fun onConnectionStateChange(gatt: BluetoothGatt, status: Int, newState: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                main.post { listeners.onError?.invoke("BLE 连接失败：status=$status；确认 Mac 网关已停止") }
                return
            }
            if (newState == BluetoothProfile.STATE_CONNECTED) {
                main.post { listeners.onState?.invoke("已连接，协商 MTU…") }
                gatt.requestMtu(247)
            } else if (newState == BluetoothProfile.STATE_DISCONNECTED) {
                main.post { listeners.onState?.invoke("已断开") }
                gatt.close()
                if (this@NrfBleClient.gatt === gatt) this@NrfBleClient.gatt = null
            }
        }

        @SuppressLint("MissingPermission")
        override fun onMtuChanged(gatt: BluetoothGatt, mtu: Int, status: Int) {
            main.post { listeners.onState?.invoke("MTU=$mtu，发现服务…") }
            gatt.discoverServices()
        }

        @SuppressLint("MissingPermission")
        override fun onServicesDiscovered(gatt: BluetoothGatt, status: Int) {
            if (status != BluetoothGatt.GATT_SUCCESS) {
                main.post { listeners.onError?.invoke("服务发现失败：status=$status") }
                return
            }
            val service = gatt.getService(serviceUuid)
            val data = service?.getCharacteristic(dataUuid)
            ackCharacteristic = service?.getCharacteristic(ackUuid)
            if (data == null || ackCharacteristic == null) {
                main.post { listeners.onError?.invoke("未找到 SenseMind 服务/特征；固件版本或 UUID 不匹配") }
                return
            }
            gatt.setCharacteristicNotification(data, true)
            val descriptor = data.getDescriptor(
                UUID.fromString("00002902-0000-1000-8000-00805f9b34fb"))
            if (descriptor == null) {
                main.post { listeners.onError?.invoke("通知配置描述符缺失") }
                return
            }
            descriptor.value = BluetoothGattDescriptor.ENABLE_NOTIFICATION_VALUE
            @Suppress("DEPRECATION")
            gatt.writeDescriptor(descriptor)
            main.post { listeners.onState?.invoke("已订阅原始数据通知；等待固件数据") }
        }

        override fun onCharacteristicChanged(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
            value: ByteArray,
        ) {
            handleNotification(value)
        }

        @Deprecated("Deprecated in API 33; retained for API 26-32")
        override fun onCharacteristicChanged(
            gatt: BluetoothGatt,
            characteristic: BluetoothGattCharacteristic,
        ) {
            @Suppress("DEPRECATION")
            handleNotification(characteristic.value ?: return)
        }
    }

    private fun handleNotification(frame: ByteArray) {
        try {
            val complete = assembler.feed(frame) ?: return
            val text = complete.payload.toString(Charsets.UTF_8)
            main.post { listeners.onPacket?.invoke(complete.messageId, text) }
        } catch (error: Exception) {
            main.post { listeners.onError?.invoke("分片无效：${error.message ?: "CRC/长度错误"}") }
        }
    }

    @SuppressLint("MissingPermission")
    fun sendAck(messageId: Long, retry: Boolean = false) {
        val gatt = gatt ?: return
        val characteristic = ackCharacteristic ?: return
        characteristic.writeType = BluetoothGattCharacteristic.WRITE_TYPE_DEFAULT
        @Suppress("DEPRECATION")
        characteristic.value = BleProtocol.acknowledgement(messageId, retry)
        @Suppress("DEPRECATION")
        gatt.writeCharacteristic(characteristic)
    }

    @SuppressLint("MissingPermission")
    fun disconnect() {
        stopScan()
        try {
            gatt?.disconnect()
            gatt?.close()
        } catch (_: SecurityException) {
            // Permission revoked; nothing else can be done safely.
        }
        gatt = null
        ackCharacteristic = null
    }
}
