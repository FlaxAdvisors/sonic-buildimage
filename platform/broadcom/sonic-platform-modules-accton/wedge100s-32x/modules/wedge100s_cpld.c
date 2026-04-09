/**
 * @file wedge100s_cpld.c
 * @brief CPLD I2C driver for Accton Wedge 100S-32X
 *
 * Single CPLD at i2c-1/0x32.
 *
 * Sysfs attributes (under /sys/bus/i2c/devices/1-0032/):
 *   cpld_version  (RO) — "major.minor" from regs 0x00/0x01
 *   psu1_present  (RO) — 1 = present, 0 = absent  (bit 0 of 0x10, active-low)
 *   psu1_pgood    (RO) — 1 = power good            (bit 1 of 0x10, active-high)
 *   psu2_present  (RO) — 1 = present, 0 = absent  (bit 4 of 0x10, active-low)
 *   psu2_pgood    (RO) — 1 = power good            (bit 5 of 0x10, active-high)
 *   psu1_input_ok (RO) — 1 = input OK, 0 = bad    (bit 2 of 0x10, active-high)
 *   psu1_alarm    (RO) — 1 = normal, 0 = alarm     (bit 3 of 0x10, active-high)
 *   psu2_input_ok (RO) — 1 = input OK, 0 = bad    (bit 6 of 0x10, active-high)
 *   psu2_alarm    (RO) — 1 = normal, 0 = alarm     (bit 7 of 0x10, active-high)
 *   board_rev     (RO) — 4-bit board revision       (bits [3:0] of 0x00)
 *   model_id      (RO) — 2-bit model ID             (bits [5:4] of 0x00; 0=wedge100 TOR)
 *   pwr_stby_ok   (RO) — 1 = standby power OK       (bit 0 of 0x11)
 *   pwr_status2   (RO) — power rail status byte      (reg 0x12, hex)
 *   led_sys1      (RW) — SYS LED 1 byte (reg 0x3e): 0=off,1=red,2=green,4=blue,+8=blink
 *   led_sys2      (RW) — SYS LED 2 byte (reg 0x3f): same encoding
 *
 * Register map (from ONL ledi.c / psui.c, verified on hardware 2026-02-25):
 *   0x00  CPLD version major / board rev [3:0] / model ID [5:4]
 *   0x01  CPLD version minor
 *   0x10  PSU presence/status
 *   0x11  Power status 1 (standby power OK)
 *   0x12  Power status 2 (VCORE/VANLOG/V3V3 ready + hot)
 *   0x3e  SYS LED 1
 *   0x3f  SYS LED 2
 *
 * Pattern: accton_i2c_cpld.c (common/modules/) + leds-accton_as7712_32x.c
 *
 * Copyright (C) 2024 Accton Technology Corporation.
 * GPL v2.
 */

#include <linux/kernel.h>
#include <linux/module.h>
#include <linux/init.h>
#include <linux/slab.h>
#include <linux/i2c.h>
#include <linux/hwmon-sysfs.h>
#include <linux/mutex.h>
#include <linux/delay.h>

#define DRVNAME "wedge100s_cpld"

/* CPLD register map */
#define REG_VERSION_MAJOR  0x00
#define REG_VERSION_MINOR  0x01
#define REG_PSU_STATUS     0x10
#define REG_BOARD_REV      0x00  /* D[3:0]=BRD_REV, D[5:4]=MODEL_ID */
#define REG_PWR_STATUS1    0x11  /* D[0]=PWR_STBY_OK */
#define REG_PWR_STATUS2    0x12  /* D[0]=VCORE_VRDY, D[1]=VCORE_HOT, D[2]=VANLOG_VRDY,
                                  * D[3]=VANLOG_HOT, D[4]=V3V3_VRDY, D[5]=V3V3_HOT */
#define REG_LED_SYS1       0x3e
#define REG_LED_SYS2       0x3f

/* PSU status register bit positions (reg 0x10) */
#define PSU1_PRESENT_BIT   0  /* 0 = present, 1 = absent */
#define PSU1_PGOOD_BIT     1  /* 1 = power good */
#define PSU2_PRESENT_BIT   4  /* 0 = present, 1 = absent */
#define PSU2_PGOOD_BIT     5  /* 1 = power good */
#define PSU1_INPUT_OK_BIT  2  /* 0 = input bad, 1 = input OK */
#define PSU1_ALARM_BIT     3  /* 0 = alarm active, 1 = normal */
#define PSU2_INPUT_OK_BIT  6  /* 0 = input bad, 1 = input OK */
#define PSU2_ALARM_BIT     7  /* 0 = alarm active, 1 = normal */

#define I2C_RW_RETRY_COUNT    10
#define I2C_RW_RETRY_INTERVAL 60  /* ms */

struct wedge100s_cpld_data {
	struct mutex update_lock;
};

/* ---------------------------------------------------------------------------
 * I2C helpers with retry
 * --------------------------------------------------------------------------- */

/**
 * @brief Read a single byte from a CPLD register via I2C with retry.
 * @param client I2C client handle for the CPLD device.
 * @param reg Register address to read.
 * @return Register value (0–255) on success, negative errno on failure.
 */
static int cpld_read(struct i2c_client *client, u8 reg)
{
	int status, retry = I2C_RW_RETRY_COUNT;

	while (retry--) {
		status = i2c_smbus_read_byte_data(client, reg);
		if (status >= 0)
			return status;
		msleep(I2C_RW_RETRY_INTERVAL);
	}
	return status;
}

/**
 * @brief Write a single byte to a CPLD register via I2C with retry.
 * @param client I2C client handle for the CPLD device.
 * @param reg Register address to write.
 * @param value Byte value to write.
 * @return 0 on success, negative errno on failure.
 */
static int cpld_write(struct i2c_client *client, u8 reg, u8 value)
{
	int status, retry = I2C_RW_RETRY_COUNT;

	while (retry--) {
		status = i2c_smbus_write_byte_data(client, reg, value);
		if (status >= 0)
			return status;
		msleep(I2C_RW_RETRY_INTERVAL);
	}
	return status;
}

/* ---------------------------------------------------------------------------
 * sysfs show/store handlers
 * --------------------------------------------------------------------------- */

/** @brief Show CPLD firmware version as "major.minor". */
static ssize_t show_cpld_version(struct device *dev,
				 struct device_attribute *attr, char *buf)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	int major, minor;

	mutex_lock(&data->update_lock);
	major = cpld_read(client, REG_VERSION_MAJOR);
	minor = cpld_read(client, REG_VERSION_MINOR);
	mutex_unlock(&data->update_lock);

	if (major < 0)
		return major;
	if (minor < 0)
		return minor;

	return scnprintf(buf, PAGE_SIZE, "%d.%d\n", major, minor);
}

/** @brief Show PSU1 presence (1=present, 0=absent). Active-low bit 0 of reg 0x10. */
static ssize_t show_psu1_present(struct device *dev,
				  struct device_attribute *attr, char *buf)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	int val;

	mutex_lock(&data->update_lock);
	val = cpld_read(client, REG_PSU_STATUS);
	mutex_unlock(&data->update_lock);

	if (val < 0)
		return val;
	/* bit 0: 0 = present → return 1 when present */
	return scnprintf(buf, PAGE_SIZE, "%d\n",
			 !((val >> PSU1_PRESENT_BIT) & 1));
}

/** @brief Show PSU1 power-good status (1=good). Active-high bit 1 of reg 0x10. */
static ssize_t show_psu1_pgood(struct device *dev,
				struct device_attribute *attr, char *buf)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	int val;

	mutex_lock(&data->update_lock);
	val = cpld_read(client, REG_PSU_STATUS);
	mutex_unlock(&data->update_lock);

	if (val < 0)
		return val;
	return scnprintf(buf, PAGE_SIZE, "%d\n",
			 (val >> PSU1_PGOOD_BIT) & 1);
}

/** @brief Show PSU2 presence (1=present, 0=absent). Active-low bit 4 of reg 0x10. */
static ssize_t show_psu2_present(struct device *dev,
				  struct device_attribute *attr, char *buf)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	int val;

	mutex_lock(&data->update_lock);
	val = cpld_read(client, REG_PSU_STATUS);
	mutex_unlock(&data->update_lock);

	if (val < 0)
		return val;
	/* bit 4: 0 = present → return 1 when present */
	return scnprintf(buf, PAGE_SIZE, "%d\n",
			 !((val >> PSU2_PRESENT_BIT) & 1));
}

/** @brief Show PSU2 power-good status (1=good). Active-high bit 5 of reg 0x10. */
static ssize_t show_psu2_pgood(struct device *dev,
				struct device_attribute *attr, char *buf)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	int val;

	mutex_lock(&data->update_lock);
	val = cpld_read(client, REG_PSU_STATUS);
	mutex_unlock(&data->update_lock);

	if (val < 0)
		return val;
	return scnprintf(buf, PAGE_SIZE, "%d\n",
			 (val >> PSU2_PGOOD_BIT) & 1);
}

/** @brief Show PSU1 alarm status (1=normal, 0=alarm). Active-high bit 3 of reg 0x10. */
static ssize_t show_psu1_alarm(struct device *dev,
				struct device_attribute *attr, char *buf)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	int val;

	mutex_lock(&data->update_lock);
	val = cpld_read(client, REG_PSU_STATUS);
	mutex_unlock(&data->update_lock);

	if (val < 0)
		return val;
	return scnprintf(buf, PAGE_SIZE, "%d\n",
			 (val >> PSU1_ALARM_BIT) & 1);
}

/** @brief Show PSU1 input-OK status (1=input OK, 0=bad). Active-high bit 2 of reg 0x10. */
static ssize_t show_psu1_input_ok(struct device *dev,
				   struct device_attribute *attr, char *buf)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	int val;

	mutex_lock(&data->update_lock);
	val = cpld_read(client, REG_PSU_STATUS);
	mutex_unlock(&data->update_lock);

	if (val < 0)
		return val;
	return scnprintf(buf, PAGE_SIZE, "%d\n",
			 (val >> PSU1_INPUT_OK_BIT) & 1);
}

/** @brief Show PSU2 alarm status (1=normal, 0=alarm). Active-high bit 7 of reg 0x10. */
static ssize_t show_psu2_alarm(struct device *dev,
				struct device_attribute *attr, char *buf)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	int val;

	mutex_lock(&data->update_lock);
	val = cpld_read(client, REG_PSU_STATUS);
	mutex_unlock(&data->update_lock);

	if (val < 0)
		return val;
	return scnprintf(buf, PAGE_SIZE, "%d\n",
			 (val >> PSU2_ALARM_BIT) & 1);
}

/** @brief Show PSU2 input-OK status (1=input OK, 0=bad). Active-high bit 6 of reg 0x10. */
static ssize_t show_psu2_input_ok(struct device *dev,
				   struct device_attribute *attr, char *buf)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	int val;

	mutex_lock(&data->update_lock);
	val = cpld_read(client, REG_PSU_STATUS);
	mutex_unlock(&data->update_lock);

	if (val < 0)
		return val;
	return scnprintf(buf, PAGE_SIZE, "%d\n",
			 (val >> PSU2_INPUT_OK_BIT) & 1);
}

/** @brief Show board revision (4-bit BRD_REV from bits [3:0] of reg 0x00). */
static ssize_t show_board_rev(struct device *dev,
			      struct device_attribute *attr, char *buf)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	int val;

	mutex_lock(&data->update_lock);
	val = cpld_read(client, REG_BOARD_REV);
	mutex_unlock(&data->update_lock);

	if (val < 0)
		return val;
	return scnprintf(buf, PAGE_SIZE, "%d\n", val & 0x0f);
}

/** @brief Show model ID (2-bit MODEL_ID from bits [5:4] of reg 0x00; 0=wedge100 TOR). */
static ssize_t show_model_id(struct device *dev,
			     struct device_attribute *attr, char *buf)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	int val;

	mutex_lock(&data->update_lock);
	val = cpld_read(client, REG_BOARD_REV);
	mutex_unlock(&data->update_lock);

	if (val < 0)
		return val;
	return scnprintf(buf, PAGE_SIZE, "%d\n", (val >> 4) & 0x03);
}

/** @brief Show standby power OK status (1=OK). Bit 0 of reg 0x11. */
static ssize_t show_pwr_stby_ok(struct device *dev,
				struct device_attribute *attr, char *buf)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	int val;

	mutex_lock(&data->update_lock);
	val = cpld_read(client, REG_PWR_STATUS1);
	mutex_unlock(&data->update_lock);

	if (val < 0)
		return val;
	return scnprintf(buf, PAGE_SIZE, "%d\n", val & 1);
}

/** @brief Show power status 2 register (reg 0x12) as hex — caller decodes bits. */
static ssize_t show_pwr_status2(struct device *dev,
				struct device_attribute *attr, char *buf)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	int val;

	mutex_lock(&data->update_lock);
	val = cpld_read(client, REG_PWR_STATUS2);
	mutex_unlock(&data->update_lock);

	if (val < 0)
		return val;
	return scnprintf(buf, PAGE_SIZE, "0x%02x\n", val);
}

/** @brief Show SYS LED 1 register value (reg 0x3e) as hex. */
static ssize_t show_led_sys1(struct device *dev,
			      struct device_attribute *attr, char *buf)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	int val;

	mutex_lock(&data->update_lock);
	val = cpld_read(client, REG_LED_SYS1);
	mutex_unlock(&data->update_lock);

	if (val < 0)
		return val;
	return scnprintf(buf, PAGE_SIZE, "0x%02x\n", val);
}

/** @brief Store SYS LED 1 value (0x00–0xff) to reg 0x3e. */
static ssize_t store_led_sys1(struct device *dev,
			       struct device_attribute *attr,
			       const char *buf, size_t count)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	unsigned long val;
	int status;

	status = kstrtoul(buf, 0, &val);
	if (status)
		return status;
	if (val > 0xff)
		return -EINVAL;

	mutex_lock(&data->update_lock);
	status = cpld_write(client, REG_LED_SYS1, (u8)val);
	mutex_unlock(&data->update_lock);

	return status < 0 ? status : count;
}

/** @brief Show SYS LED 2 register value (reg 0x3f) as hex. */
static ssize_t show_led_sys2(struct device *dev,
			      struct device_attribute *attr, char *buf)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	int val;

	mutex_lock(&data->update_lock);
	val = cpld_read(client, REG_LED_SYS2);
	mutex_unlock(&data->update_lock);

	if (val < 0)
		return val;
	return scnprintf(buf, PAGE_SIZE, "0x%02x\n", val);
}

/** @brief Store SYS LED 2 value (0x00–0xff) to reg 0x3f. */
static ssize_t store_led_sys2(struct device *dev,
			       struct device_attribute *attr,
			       const char *buf, size_t count)
{
	struct i2c_client *client = to_i2c_client(dev);
	struct wedge100s_cpld_data *data = i2c_get_clientdata(client);
	unsigned long val;
	int status;

	status = kstrtoul(buf, 0, &val);
	if (status)
		return status;
	if (val > 0xff)
		return -EINVAL;

	mutex_lock(&data->update_lock);
	status = cpld_write(client, REG_LED_SYS2, (u8)val);
	mutex_unlock(&data->update_lock);

	return status < 0 ? status : count;
}

/* ---------------------------------------------------------------------------
 * Attribute group
 * --------------------------------------------------------------------------- */

static DEVICE_ATTR(cpld_version, S_IRUGO, show_cpld_version, NULL);
static DEVICE_ATTR(psu1_present, S_IRUGO, show_psu1_present, NULL);
static DEVICE_ATTR(psu1_pgood,   S_IRUGO, show_psu1_pgood,   NULL);
static DEVICE_ATTR(psu2_present, S_IRUGO, show_psu2_present, NULL);
static DEVICE_ATTR(psu2_pgood,   S_IRUGO, show_psu2_pgood,   NULL);
static DEVICE_ATTR(psu1_alarm,    S_IRUGO, show_psu1_alarm,    NULL);
static DEVICE_ATTR(psu1_input_ok, S_IRUGO, show_psu1_input_ok, NULL);
static DEVICE_ATTR(psu2_alarm,    S_IRUGO, show_psu2_alarm,    NULL);
static DEVICE_ATTR(psu2_input_ok, S_IRUGO, show_psu2_input_ok, NULL);
static DEVICE_ATTR(board_rev,    S_IRUGO, show_board_rev,    NULL);
static DEVICE_ATTR(model_id,     S_IRUGO, show_model_id,     NULL);
static DEVICE_ATTR(pwr_stby_ok,  S_IRUGO, show_pwr_stby_ok,  NULL);
static DEVICE_ATTR(pwr_status2,  S_IRUGO, show_pwr_status2,  NULL);
static DEVICE_ATTR(led_sys1, S_IRUGO | S_IWUSR, show_led_sys1, store_led_sys1);
static DEVICE_ATTR(led_sys2, S_IRUGO | S_IWUSR, show_led_sys2, store_led_sys2);

static struct attribute *wedge100s_cpld_attrs[] = {
	&dev_attr_cpld_version.attr,
	&dev_attr_psu1_present.attr,
	&dev_attr_psu1_pgood.attr,
	&dev_attr_psu2_present.attr,
	&dev_attr_psu2_pgood.attr,
	&dev_attr_psu1_alarm.attr,
	&dev_attr_psu1_input_ok.attr,
	&dev_attr_psu2_alarm.attr,
	&dev_attr_psu2_input_ok.attr,
	&dev_attr_board_rev.attr,
	&dev_attr_model_id.attr,
	&dev_attr_pwr_stby_ok.attr,
	&dev_attr_pwr_status2.attr,
	&dev_attr_led_sys1.attr,
	&dev_attr_led_sys2.attr,
	NULL,
};

static const struct attribute_group wedge100s_cpld_group = {
	.attrs = wedge100s_cpld_attrs,
};

/* ---------------------------------------------------------------------------
 * I2C driver probe / remove
 * --------------------------------------------------------------------------- */

/**
 * @brief Probe callback — allocate driver data and create sysfs group.
 * @param client I2C client for the CPLD at 0x32.
 * @return 0 on success, negative errno on failure.
 */
static int wedge100s_cpld_probe(struct i2c_client *client)
{
	struct wedge100s_cpld_data *data;
	int status;

	if (!i2c_check_functionality(client->adapter,
				     I2C_FUNC_SMBUS_BYTE_DATA)) {
		dev_err(&client->dev, "SMBUS byte data not supported\n");
		return -EIO;
	}

	data = devm_kzalloc(&client->dev, sizeof(*data), GFP_KERNEL);
	if (!data)
		return -ENOMEM;

	mutex_init(&data->update_lock);
	i2c_set_clientdata(client, data);

	status = sysfs_create_group(&client->dev.kobj, &wedge100s_cpld_group);
	if (status) {
		dev_err(&client->dev, "sysfs_create_group failed (%d)\n",
			status);
		return status;
	}

	dev_info(&client->dev, "wedge100s CPLD at 0x%02x\n", client->addr);
	return 0;
}

/** @brief Remove callback — tear down sysfs group. */
static void wedge100s_cpld_remove(struct i2c_client *client)
{
	sysfs_remove_group(&client->dev.kobj, &wedge100s_cpld_group);
}

static const struct i2c_device_id wedge100s_cpld_id[] = {
	{ DRVNAME, 0 },
	{ }
};
MODULE_DEVICE_TABLE(i2c, wedge100s_cpld_id);

static struct i2c_driver wedge100s_cpld_driver = {
	.driver = {
		.name = DRVNAME,
	},
	.probe    = wedge100s_cpld_probe,
	.remove   = wedge100s_cpld_remove,
	.id_table = wedge100s_cpld_id,
};

module_i2c_driver(wedge100s_cpld_driver);

MODULE_AUTHOR("Accton Technology Corporation");
MODULE_DESCRIPTION("CPLD driver for Accton Wedge 100S-32X");
MODULE_LICENSE("GPL");
